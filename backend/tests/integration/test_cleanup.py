"""The hourly cleanup against real Postgres (Phase 2 Step 3).

Rows are "aged" by giving them an old `created_at` / `expires_at`, and the
cleanup is told what time it is (`now=`), so nothing waits for a clock.
Every rule is tested from both sides: what it must clean up, and what it
must leave alone - the second half is where a cleanup job does damage.

The test that matters most is the last one: deleting refresh tokens must
not switch off reuse detection (see `_delete_dead_token_families`).
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import structlog
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.logging import redact_sensitive_fields
from app.core.security import REFRESH_TOKEN_TTL
from app.models import (
    Account,
    AccountType,
    BankCode,
    RefreshToken,
    StatementUpload,
    UploadBlob,
    UploadStatus,
    User,
)
from app.parsers.base import FileType
from app.services import cleanup as cleanup_service
from app.services.auth import REUSE_GRACE_PERIOD
from app.services.imports import STALE_UPLOAD_AFTER, STALE_UPLOAD_MESSAGE
from app.workers import jobs

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)
JUST_STALE = NOW - STALE_UPLOAD_AFTER - timedelta(minutes=1)
NOT_YET_STALE = NOW - STALE_UPLOAD_AFTER + timedelta(minutes=5)
JUST_EXPIRED_FILE = NOW - cleanup_service.BLOB_MAX_AGE - timedelta(minutes=1)


# --- Fixtures and helpers ---------------------------------------------------


@pytest.fixture
def account(db: Session) -> Account:
    user = User(email="alice@example.com", password_hash="not-a-real-hash", name="Alice")
    db.add(user)
    db.flush()
    account = Account(
        user_id=user.id,
        bank_code=BankCode.ICICI,
        nickname="Salary account",
        account_type=AccountType.SAVINGS,
        masked_number="1234",
    )
    db.add(account)
    db.commit()
    return account


def add_upload(
    db: Session,
    account: Account,
    *,
    status: UploadStatus,
    created_at: datetime,
    file_created_at: datetime | None = None,
) -> uuid.UUID:
    """An upload created at `created_at`; with a stored file if `file_created_at`."""
    upload = StatementUpload(
        user_id=account.user_id,
        account_id=account.id,
        original_filename="statement.xls",
        file_type=FileType.XLS,
        # A different (valid) hash per upload, so the file-hash index never
        # gets in the way of these tests.
        file_sha256=uuid.uuid4().hex * 2,
        status=status,
        created_at=created_at,
    )
    db.add(upload)
    db.flush()
    if file_created_at is not None:
        db.add(
            UploadBlob(
                upload_id=upload.id,
                encrypted_content=b"ciphertext",
                nonce=b"n" * 12,
                created_at=file_created_at,
            )
        )
    db.commit()
    return upload.id


def reload(db: Session, upload_id: uuid.UUID) -> StatementUpload:
    db.expire_all()
    return db.get_one(StatementUpload, upload_id)


def has_file(db: Session, upload_id: uuid.UUID) -> bool:
    return db.get(UploadBlob, upload_id) is not None


def add_token(
    db: Session, account: Account, *, family_id: uuid.UUID, expires_at: datetime
) -> uuid.UUID:
    token = RefreshToken(
        user_id=account.user_id,
        token_hash=uuid.uuid4().hex,
        family_id=family_id,
        expires_at=expires_at,
    )
    db.add(token)
    db.commit()
    return token.id


def count_tokens(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(RefreshToken)) or 0


# --- Rule 1: stale uploads ------------------------------------------------------


@pytest.mark.parametrize("status", [UploadStatus.PENDING, UploadStatus.PROCESSING])
def test_an_upload_unfinished_for_over_an_hour_is_failed(
    db: Session, account: Account, status: UploadStatus
) -> None:
    """A lost job: failed with a message, its stored file deleted."""
    upload_id = add_upload(
        db, account, status=status, created_at=JUST_STALE, file_created_at=JUST_STALE
    )

    counts = cleanup_service.run_cleanup(db, now=NOW)

    upload = reload(db, upload_id)
    assert upload.status is UploadStatus.FAILED
    assert upload.error_message == STALE_UPLOAD_MESSAGE
    assert not has_file(db, upload_id)
    assert (counts.stale_uploads_failed, counts.blobs_deleted) == (1, 1)


def test_a_recent_unfinished_upload_is_left_alone(db: Session, account: Account) -> None:
    """Five minutes short of stale: its job may still be in RQ's retry backoff."""
    upload_id = add_upload(
        db,
        account,
        status=UploadStatus.PENDING,
        created_at=NOT_YET_STALE,
        file_created_at=NOT_YET_STALE,
    )

    counts = cleanup_service.run_cleanup(db, now=NOW)

    assert reload(db, upload_id).status is UploadStatus.PENDING
    assert has_file(db, upload_id)
    assert counts == cleanup_service.CleanupCounts(0, 0, 0, 0)


@pytest.mark.parametrize(
    "status", [UploadStatus.COMPLETED, UploadStatus.NEEDS_REVIEW, UploadStatus.FAILED]
)
def test_an_old_finished_upload_is_left_alone(
    db: Session, account: Account, status: UploadStatus
) -> None:
    """Age alone never touches a finished import."""
    upload_id = add_upload(db, account, status=status, created_at=NOW - timedelta(days=90))

    cleanup_service.run_cleanup(db, now=NOW)

    upload = reload(db, upload_id)
    assert upload.status is status
    assert upload.error_message is None


# --- Rule 2: old stored files ----------------------------------------------------


def test_an_upload_waiting_for_its_password_expires_after_a_day(
    db: Session, account: Account
) -> None:
    """`needs_password` is not a lost job - it waits for the user - so the
    one-hour rule leaves it alone. Its file still may not be kept beyond
    24 hours (SPEC §5), and then the upload has nothing left to import."""
    waiting = add_upload(
        db,
        account,
        status=UploadStatus.NEEDS_PASSWORD,
        created_at=JUST_EXPIRED_FILE,
        file_created_at=JUST_EXPIRED_FILE,
    )

    counts = cleanup_service.run_cleanup(db, now=NOW)

    upload = reload(db, waiting)
    assert upload.status is UploadStatus.FAILED
    assert upload.error_message == cleanup_service.UPLOAD_EXPIRED_MESSAGE
    assert not has_file(db, waiting)
    assert (counts.stale_uploads_failed, counts.expired_uploads_failed) == (0, 1)


def test_an_upload_waiting_for_its_password_for_two_hours_is_left_alone(
    db: Session, account: Account
) -> None:
    two_hours_ago = NOW - timedelta(hours=2)
    waiting = add_upload(
        db,
        account,
        status=UploadStatus.NEEDS_PASSWORD,
        created_at=two_hours_ago,
        file_created_at=two_hours_ago,
    )

    cleanup_service.run_cleanup(db, now=NOW)

    assert reload(db, waiting).status is UploadStatus.NEEDS_PASSWORD
    assert has_file(db, waiting)


def test_a_leftover_file_of_a_finished_upload_is_deleted(db: Session, account: Account) -> None:
    """Should not exist (a finished import deletes its file in the same
    commit), but if one is left, it is deleted and the import stays as it is."""
    upload_id = add_upload(
        db,
        account,
        status=UploadStatus.COMPLETED,
        created_at=JUST_EXPIRED_FILE,
        file_created_at=JUST_EXPIRED_FILE,
    )

    counts = cleanup_service.run_cleanup(db, now=NOW)

    assert reload(db, upload_id).status is UploadStatus.COMPLETED
    assert not has_file(db, upload_id)
    assert (counts.expired_uploads_failed, counts.blobs_deleted) == (0, 1)


# --- Running it again -------------------------------------------------------------


def test_a_second_run_finds_nothing_to_do(db: Session, account: Account) -> None:
    """Idempotent: the scheduler runs it every hour regardless."""
    add_upload(
        db, account, status=UploadStatus.PENDING, created_at=JUST_STALE, file_created_at=JUST_STALE
    )
    add_token(db, account, family_id=uuid.uuid4(), expires_at=NOW - timedelta(days=1))
    first = cleanup_service.run_cleanup(db, now=NOW)
    assert first != cleanup_service.CleanupCounts(0, 0, 0, 0)

    second = cleanup_service.run_cleanup(db, now=NOW)

    assert second == cleanup_service.CleanupCounts(0, 0, 0, 0)


def test_the_job_cleans_up_and_logs_counts_only(
    db: Session, account: Account, job_sessions: sessionmaker[Session]
) -> None:
    """The RQ job: its own session, the real time, one log line of counts."""
    upload_id = add_upload(
        db, account, status=UploadStatus.PENDING, created_at=JUST_STALE, file_created_at=JUST_STALE
    )

    with structlog.testing.capture_logs() as events:
        jobs.cleanup()

    assert reload(db, upload_id).status is UploadStatus.FAILED
    [finished] = [event for event in events if event["event"] == "cleanup_finished"]
    assert finished["stale_uploads_failed"] == 1
    assert str(upload_id) not in str(events)


def test_the_counts_survive_log_redaction(db: Session, account: Account) -> None:
    """`capture_logs` skips our processors, so it cannot see redaction.
    The real processor is applied by hand: a count named like a secret
    ("refresh_tokens_deleted" contains "token") would come out as
    [REDACTED] and the log line would be useless. Found in the live run."""
    with structlog.testing.capture_logs() as events:
        cleanup_service.run_cleanup(db, now=NOW)

    [finished] = [event for event in events if event["event"] == "cleanup_finished"]
    assert redact_sensitive_fields(None, "info", dict(finished)) == finished


# --- Rule 3: refresh tokens -------------------------------------------------------


def test_a_family_whose_every_token_expired_is_deleted(db: Session, account: Account) -> None:
    family = uuid.uuid4()
    add_token(db, account, family_id=family, expires_at=NOW - timedelta(days=40))
    add_token(db, account, family_id=family, expires_at=NOW - timedelta(days=2))

    counts = cleanup_service.run_cleanup(db, now=NOW)

    assert counts.refresh_tokens_deleted == 2
    assert count_tokens(db) == 0


def test_an_expired_token_is_kept_while_its_family_is_alive(db: Session, account: Account) -> None:
    """The old token is the evidence reuse detection needs, as long as a
    newer token of its family can still be used."""
    family = uuid.uuid4()
    add_token(db, account, family_id=family, expires_at=NOW - timedelta(days=2))
    add_token(db, account, family_id=family, expires_at=NOW + timedelta(days=20))
    other_dead_family = uuid.uuid4()
    add_token(db, account, family_id=other_dead_family, expires_at=NOW - timedelta(days=1))

    counts = cleanup_service.run_cleanup(db, now=NOW)

    assert counts.refresh_tokens_deleted == 1
    remaining = set(db.scalars(select(RefreshToken.family_id)))
    assert remaining == {family}


def test_reuse_detection_still_works_after_cleanup(client: TestClient, db: Session) -> None:
    """The whole point of keeping live families, end to end through the API.

    A thief refreshed with the stolen token T1 and holds T2. The real user
    comes back with T1 after T1 itself has expired. The cleanup runs in
    between. The replay must still be recognised, and T2 - the thief's
    token - must stop working. Had the cleanup deleted the expired T1, the
    replay would be just an unknown token, and T2 would go on working.
    """
    credentials = {"email": "bob@example.com", "password": "correct horse battery"}
    client.post("/api/v1/auth/register", json={**credentials, "name": "Bob"})
    token_1 = client.post("/api/v1/auth/login", json=credentials).cookies["refresh_token"]

    def refresh(token: str) -> Response:
        """Send exactly this refresh token, like the browser (or thief) holding it."""
        client.cookies.clear()
        return client.post("/api/v1/auth/refresh", headers={"Cookie": f"refresh_token={token}"})

    token_2 = refresh(token_1).cookies["refresh_token"]  # the thief's refresh

    # T1 was rotated long ago and has expired since; T2 is still valid.
    long_ago = datetime.now(UTC) - REFRESH_TOKEN_TTL - REUSE_GRACE_PERIOD - timedelta(days=1)
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.revoked_at.is_not(None))
        .values(revoked_at=long_ago, expires_at=long_ago + timedelta(hours=1))
    )
    db.commit()

    cleanup_service.run_cleanup(db, now=datetime.now(UTC))
    # The expired T1 survived the cleanup, because T2 keeps the family alive.
    assert count_tokens(db) == 2

    assert refresh(token_1).status_code == 401
    # Recognised as reuse, so the whole family was revoked: T2 is dead too.
    assert refresh(token_2).status_code == 401
