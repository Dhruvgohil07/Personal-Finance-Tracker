"""Password-protected PDF statements end to end (Phase 2 Step 10, ADR 020).

The two ways a password can arrive (SPEC §6.1):

    POST /uploads + password      -> unlocked in the request, queued (202)
    POST /uploads, no password    -> `needs_password`, 409 PASSWORD_REQUIRED
      then POST /uploads/{id}/password -> unlocked, queued (202)

and the rule this step exists to keep (CLAUDE.md §7.2): the password is
never persisted - not in any database table, not in Redis (job arguments
or anything else), not in the logs, not in a response.
`test_the_password_is_never_stored_logged_or_echoed` is the proof.

Locked files are synthetic ICICI statements encrypted by fpdf2
(`build_icici_pdf(password=...)`), a different library from the pypdf the
app unlocks with. All names, numbers and passwords are invented (rule 8).
"""

import logging
import uuid
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient
from httpx import Response
from pypdf import PdfWriter
from redis import Redis
from rq import Queue
from sqlalchemy import Engine, delete, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.crypto import EncryptedFile, decrypt_file
from app.core.redis import get_redis
from app.db.base import Base
from app.models import StatementUpload, UploadBlob, UploadStatus
from app.parsers.readers import LOCKED_PDF_MESSAGE, PDF_UNREADABLE_MESSAGE
from app.schemas.upload import MAX_PDF_PASSWORD_LENGTH
from app.services import cleanup as cleanup_service
from app.services import imports as imports_service
from app.workers import jobs
from app.workers.queue import get_queue
from tests.fixtures.icici_pdf import build_icici_pdf, encrypt_pdf, is_encrypted
from tests.integration.test_categorization import ROWS as UPI_ROWS
from tests.integration.test_upload_worker import count, upload_row
from tests.integration.test_uploads import (
    UPLOADS,
    AuthHeadersFactory,
    get_upload,
    make_account,
    post_upload,
    statement_bytes,
    transactions_of,
)

pytestmark = pytest.mark.integration

# Distinctive, so finding it anywhere it should not be is unambiguous.
PASSWORD = "Kh4rcha-Pdf-0101"
WRONG_PASSWORD = "Wr0ng-Guess-77"


@pytest.fixture
def alice(make_auth_headers: AuthHeadersFactory) -> dict[str, str]:
    return make_auth_headers("alice@example.com")


@pytest.fixture
def bob(make_auth_headers: AuthHeadersFactory) -> dict[str, str]:
    return make_auth_headers("bob@example.com")


@pytest.fixture
def account_id(client: TestClient, alice: dict[str, str]) -> str:
    return make_account(client, alice)


@pytest.fixture
def plain_pdf(tmp_path: Path) -> bytes:
    return build_icici_pdf(tmp_path / "plain.pdf", UPI_ROWS).read_bytes()


@pytest.fixture
def locked_pdf(tmp_path: Path) -> bytes:
    """The same statement, locked with PASSWORD the way a bank sends it."""
    return build_icici_pdf(tmp_path / "locked.pdf", UPI_ROWS, password=PASSWORD).read_bytes()


def post_pdf(
    client: TestClient,
    headers: dict[str, str],
    *,
    content: bytes,
    account_id: str,
    password: str | None = None,
) -> Response:
    """`post_upload` (test_uploads.py) with a `.pdf` name."""
    return post_upload(
        client,
        headers,
        content=content,
        account_id=account_id,
        filename="statement.pdf",
        password=password,
    )


def send_password(
    client: TestClient, headers: dict[str, str], upload_id: str, password: str
) -> Response:
    return client.post(
        f"{UPLOADS}/{upload_id}/password", headers=headers, json={"password": password}
    )


def upload_waiting_for_password(
    client: TestClient, headers: dict[str, str], *, content: bytes, account_id: str
) -> str:
    """POST a locked PDF without its password; return the waiting upload's id."""
    response = post_pdf(client, headers, content=content, account_id=account_id)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PASSWORD_REQUIRED"
    upload_id: str = error["details"]["upload_id"]
    return upload_id


def stored_file(db: Session, upload_id: str) -> bytes:
    """The upload's stored file, decrypted with our key."""
    db.expire_all()
    blob = db.get_one(UploadBlob, uuid.UUID(upload_id))
    return decrypt_file(
        EncryptedFile(nonce=blob.nonce, ciphertext=blob.encrypted_content),
        uuid.UUID(upload_id),
    )


# --- The password sent with the upload --------------------------------------------


def test_a_locked_pdf_sent_with_its_password_is_imported(
    client: TestClient, alice: dict[str, str], account_id: str, locked_pdf: bytes, db: Session
) -> None:
    response = post_pdf(client, alice, content=locked_pdf, account_id=account_id, password=PASSWORD)

    assert response.status_code == 202
    body = get_upload(client, alice, response.json()["id"])
    assert body["status"] == "completed"
    assert body["rows_inserted"] == len(UPI_ROWS)
    assert body["reconciliation_errors"] == 0
    rows = transactions_of(db, body["id"])
    assert [row.raw_description for row in rows] == [row[4] for row in UPI_ROWS]


def test_the_locked_and_the_plain_copy_are_the_same_transactions(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    locked_pdf: bytes,
    plain_pdf: bytes,
) -> None:
    """Unlocking changes nothing that the fingerprint sees: the plain copy
    of the same statement, imported afterwards, is 100% duplicates."""
    first = post_pdf(client, alice, content=locked_pdf, account_id=account_id, password=PASSWORD)
    assert first.status_code == 202

    second = post_pdf(client, alice, content=plain_pdf, account_id=account_id)

    assert second.status_code == 202
    body = get_upload(client, alice, second.json()["id"])
    assert (body["rows_inserted"], body["rows_duplicate"]) == (0, len(UPI_ROWS))


def test_a_wrong_password_in_the_upload_stores_nothing(
    client: TestClient, alice: dict[str, str], account_id: str, locked_pdf: bytes, db: Session
) -> None:
    """400, and no upload row: the user just sends the upload again."""
    response = post_pdf(
        client, alice, content=locked_pdf, account_id=account_id, password=WRONG_PASSWORD
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INCORRECT_PASSWORD"
    assert (count(db, StatementUpload), count(db, UploadBlob)) == (0, 0)

    retry = post_pdf(client, alice, content=locked_pdf, account_id=account_id, password=PASSWORD)

    assert retry.status_code == 202
    assert get_upload(client, alice, retry.json()["id"])["status"] == "completed"


def test_a_pdf_with_only_an_owner_password_needs_no_password(
    client: TestClient, alice: dict[str, str], account_id: str, plain_pdf: bytes, db: Session
) -> None:
    owner_only = encrypt_pdf(plain_pdf, user_password="")

    response = post_pdf(client, alice, content=owner_only, account_id=account_id)

    assert response.status_code == 202
    assert get_upload(client, alice, response.json()["id"])["status"] == "completed"


def test_a_password_sent_with_an_xls_is_ignored(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path
) -> None:
    response = post_upload(
        client, alice, content=statement_bytes(tmp_path), account_id=account_id, password=PASSWORD
    )

    assert response.status_code == 202
    assert get_upload(client, alice, response.json()["id"])["status"] == "completed"


def test_a_too_long_password_in_the_upload_is_a_422_without_echo(
    client: TestClient, alice: dict[str, str], account_id: str, locked_pdf: bytes
) -> None:
    too_long = PASSWORD * 10
    assert len(too_long) > MAX_PDF_PASSWORD_LENGTH

    response = post_pdf(client, alice, content=locked_pdf, account_id=account_id, password=too_long)

    assert response.status_code == 422
    assert [error["field"] for error in response.json()["error"]["details"]] == ["body.password"]
    assert PASSWORD not in response.text


def test_a_pdf_that_cannot_be_rewritten_is_refused_in_the_upload(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    locked_pdf: bytes,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The password opened it, but pypdf could not write the copy: 400."""

    def broken_write(self: PdfWriter, stream: object) -> None:
        raise ValueError("cannot write")

    monkeypatch.setattr(PdfWriter, "write", broken_write)

    response = post_pdf(client, alice, content=locked_pdf, account_id=account_id, password=PASSWORD)

    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": "INVALID_STATEMENT",
        "message": PDF_UNREADABLE_MESSAGE,
        "details": None,
    }
    assert count(db, StatementUpload) == 0


def test_a_locked_pdf_the_request_could_not_open_fails_in_the_worker(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    locked_pdf: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If pypdf cannot open a file, the request passes it on unchanged
    (`unlock_pdf`), and the worker's reader names the problem."""

    def cannot_open(content: bytes, password: str | None = None) -> bytes:
        return content

    monkeypatch.setattr(imports_service, "unlock_pdf", cannot_open)

    response = post_pdf(client, alice, content=locked_pdf, account_id=account_id)

    assert response.status_code == 202
    body = get_upload(client, alice, response.json()["id"])
    assert body["status"] == "failed"
    assert body["error_message"] == LOCKED_PDF_MESSAGE


# --- No password: the upload waits for it -------------------------------------------


def test_a_locked_pdf_without_its_password_waits_for_it(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    locked_pdf: bytes,
    db: Session,
    held_queue: Queue,
) -> None:
    """409 PASSWORD_REQUIRED with the id; the LOCKED file is kept; no job."""
    upload_id = upload_waiting_for_password(
        client, alice, content=locked_pdf, account_id=account_id
    )

    response = client.get(f"{UPLOADS}/{upload_id}", headers=alice)
    assert response.json()["status"] == "needs_password"
    # Polling cannot change it, so the client is not asked to poll.
    assert "Retry-After" not in response.headers
    assert stored_file(db, upload_id) == locked_pdf
    assert held_queue.get_jobs() == []


def test_the_password_endpoint_unlocks_and_queues_the_upload(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    locked_pdf: bytes,
    db: Session,
    held_queue: Queue,
) -> None:
    upload_id = upload_waiting_for_password(
        client, alice, content=locked_pdf, account_id=account_id
    )

    response = send_password(client, alice, upload_id, PASSWORD)

    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    assert response.headers["Retry-After"] == "2"
    # The stored file is now the unlocked copy.
    assert not is_encrypted(stored_file(db, upload_id))
    [job] = held_queue.get_jobs()
    assert job.args == (upload_id,)

    jobs.process_statement(upload_id)

    body = get_upload(client, alice, upload_id)
    assert body["status"] == "completed"
    assert body["rows_inserted"] == len(UPI_ROWS)
    assert count(db, UploadBlob) == 0


def test_a_wrong_password_keeps_the_upload_waiting(
    client: TestClient, alice: dict[str, str], account_id: str, locked_pdf: bytes, db: Session
) -> None:
    upload_id = upload_waiting_for_password(
        client, alice, content=locked_pdf, account_id=account_id
    )

    response = send_password(client, alice, upload_id, WRONG_PASSWORD)

    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": "INCORRECT_PASSWORD",
        "message": "That password does not open this PDF.",
        "details": None,
    }
    assert upload_row(db, upload_id).status is UploadStatus.NEEDS_PASSWORD
    assert stored_file(db, upload_id) == locked_pdf

    # The user tries again, with the right one.
    assert send_password(client, alice, upload_id, PASSWORD).status_code == 202
    assert get_upload(client, alice, upload_id)["status"] == "completed"


@pytest.mark.parametrize("password", [None, PASSWORD], ids=["without_password", "with_password"])
def test_sending_the_locked_file_again_points_to_the_waiting_upload(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    locked_pdf: bytes,
    db: Session,
    password: str | None,
) -> None:
    """Same file = same upload: the 409 names the one already waiting, and
    a password sent along is NOT used (it belongs at /password)."""
    upload_id = upload_waiting_for_password(
        client, alice, content=locked_pdf, account_id=account_id
    )

    again = post_pdf(client, alice, content=locked_pdf, account_id=account_id, password=password)

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "PASSWORD_REQUIRED"
    assert again.json()["error"]["details"] == {"upload_id": upload_id}
    assert count(db, StatementUpload) == 1
    assert upload_row(db, upload_id).status is UploadStatus.NEEDS_PASSWORD


def test_an_upload_not_waiting_for_a_password_is_a_409(
    client: TestClient, alice: dict[str, str], account_id: str, locked_pdf: bytes
) -> None:
    """Including the second password for an upload already unlocked."""
    upload_id = upload_waiting_for_password(
        client, alice, content=locked_pdf, account_id=account_id
    )
    assert send_password(client, alice, upload_id, PASSWORD).status_code == 202

    response = send_password(client, alice, upload_id, PASSWORD)

    assert response.status_code == 409
    assert response.json()["error"]["message"] == imports_service.NOT_LOCKED_MESSAGE


def test_another_users_waiting_upload_is_not_found(
    client: TestClient,
    alice: dict[str, str],
    bob: dict[str, str],
    account_id: str,
    locked_pdf: bytes,
    db: Session,
) -> None:
    """IDOR (CLAUDE.md rule 2): even with the right password, Bob gets the
    same 404 as for an id that does not exist, and nothing changes."""
    upload_id = upload_waiting_for_password(
        client, alice, content=locked_pdf, account_id=account_id
    )

    response = send_password(client, bob, upload_id, PASSWORD)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert upload_row(db, upload_id).status is UploadStatus.NEEDS_PASSWORD
    assert stored_file(db, upload_id) == locked_pdf


def test_an_unknown_upload_is_not_found(client: TestClient, alice: dict[str, str]) -> None:
    response = send_password(client, alice, str(uuid.uuid4()), PASSWORD)

    assert response.status_code == 404


def test_the_password_endpoint_requires_authentication(client: TestClient) -> None:
    response = client.post(f"{UPLOADS}/{uuid.uuid4()}/password", json={"password": PASSWORD})

    assert response.status_code == 401


@pytest.mark.parametrize(
    "password", ["", PASSWORD * 10], ids=["empty", f"over_{MAX_PDF_PASSWORD_LENGTH}"]
)
def test_an_empty_or_too_long_password_is_a_422_without_echo(
    client: TestClient, alice: dict[str, str], password: str
) -> None:
    response = send_password(client, alice, str(uuid.uuid4()), password)

    assert response.status_code == 422
    assert [error["field"] for error in response.json()["error"]["details"]] == ["body.password"]
    assert PASSWORD not in response.text


def test_the_password_endpoint_allows_five_guesses_a_minute(
    client: TestClient, alice: dict[str, str], account_id: str, locked_pdf: bytes
) -> None:
    """UNLOCK_LIMIT: after five tries, even the right password must wait."""
    upload_id = upload_waiting_for_password(
        client, alice, content=locked_pdf, account_id=account_id
    )
    for _ in range(5):
        assert send_password(client, alice, upload_id, WRONG_PASSWORD).status_code == 400

    response = send_password(client, alice, upload_id, PASSWORD)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "RATE_LIMITED"


def test_a_waiting_upload_can_be_deleted(
    client: TestClient, alice: dict[str, str], account_id: str, locked_pdf: bytes, db: Session
) -> None:
    upload_id = upload_waiting_for_password(
        client, alice, content=locked_pdf, account_id=account_id
    )

    response = client.delete(f"{UPLOADS}/{upload_id}", headers=alice)

    assert response.status_code == 204
    assert (count(db, StatementUpload), count(db, UploadBlob)) == (0, 0)


def test_a_waiting_upload_expires_with_its_file_after_a_day(
    client: TestClient, alice: dict[str, str], account_id: str, locked_pdf: bytes, db: Session
) -> None:
    """Step 3's cleanup, on an upload made through the real flow: the
    locked file is not kept beyond 24 hours (SPEC §5), and a password sent
    after that finds nothing to unlock."""
    upload_id = upload_waiting_for_password(
        client, alice, content=locked_pdf, account_id=account_id
    )

    cleanup_service.run_cleanup(
        db,
        now=datetime.now(UTC) + cleanup_service.BLOB_MAX_AGE + timedelta(minutes=1),
        job_is_waiting=lambda _upload_id: False,
    )

    upload = upload_row(db, upload_id)
    assert upload.status is UploadStatus.FAILED
    assert upload.error_message == cleanup_service.UPLOAD_EXPIRED_MESSAGE
    assert count(db, UploadBlob) == 0
    assert send_password(client, alice, upload_id, PASSWORD).status_code == 409


# --- The password endpoint when something else goes wrong -----------------------------


def test_redis_down_on_unlock_is_a_503_and_the_upload_is_failed(
    client: TestClient, alice: dict[str, str], account_id: str, locked_pdf: bytes, db: Session
) -> None:
    """Like POST /uploads: a `pending` row with no job would never finish.
    Port 1 refuses the connection at once - a real Redis error, no mock."""
    upload_id = upload_waiting_for_password(
        client, alice, content=locked_pdf, account_id=account_id
    )
    unreachable = Queue(
        "unreachable", connection=Redis(host="127.0.0.1", port=1, socket_connect_timeout=1)
    )
    client.app.dependency_overrides[get_queue] = lambda: unreachable

    response = send_password(client, alice, upload_id, PASSWORD)

    assert response.status_code == 503
    upload = upload_row(db, upload_id)
    assert upload.status is UploadStatus.FAILED
    assert upload.error_message == imports_service.QUEUE_UNAVAILABLE_MESSAGE
    assert count(db, UploadBlob) == 0


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        ("missing", imports_service.FILE_MISSING_MESSAGE),
        ("tampered", imports_service.FILE_UNREADABLE_MESSAGE),
    ],
)
def test_a_lost_or_damaged_stored_file_fails_the_upload(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    locked_pdf: bytes,
    db: Session,
    damage: str,
    message: str,
) -> None:
    """No password can help when our own copy is gone or fails its check.
    Each case says what happened, with the worker's message for the same
    problem (review of Step 10)."""
    upload_id = upload_waiting_for_password(
        client, alice, content=locked_pdf, account_id=account_id
    )
    blob = db.get_one(UploadBlob, uuid.UUID(upload_id))
    if damage == "missing":
        db.delete(blob)
    else:
        # Flip one bit of the ciphertext: AES-GCM's tag check then fails.
        first_byte = bytes([blob.encrypted_content[0] ^ 1])
        blob.encrypted_content = first_byte + blob.encrypted_content[1:]
    db.commit()

    response = send_password(client, alice, upload_id, PASSWORD)

    assert response.status_code == 409
    assert response.json()["error"]["message"] == message
    upload = upload_row(db, upload_id)
    assert upload.status is UploadStatus.FAILED
    assert upload.error_message == message
    assert count(db, UploadBlob) == 0


def test_a_pdf_that_cannot_be_rewritten_fails_the_waiting_upload(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    locked_pdf: bytes,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retrying would fail the same way, so the upload is failed, not kept."""
    upload_id = upload_waiting_for_password(
        client, alice, content=locked_pdf, account_id=account_id
    )

    def broken_write(self: PdfWriter, stream: object) -> None:
        raise ValueError("cannot write")

    monkeypatch.setattr(PdfWriter, "write", broken_write)

    response = send_password(client, alice, upload_id, PASSWORD)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_STATEMENT"
    upload = upload_row(db, upload_id)
    assert upload.status is UploadStatus.FAILED
    assert upload.error_message == PDF_UNREADABLE_MESSAGE


# --- Review of Step 10 -------------------------------------------------------------


def oversized_copy(content: bytes, password: str | None = None) -> bytes:
    """Stands in for `unlock_pdf` writing a copy bigger than any upload may be."""
    return b"%PDF-" + b" " * get_settings().max_upload_bytes


def test_an_unlocked_copy_over_the_limit_is_refused_in_the_upload(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    locked_pdf: bytes,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What is stored must stay within the upload limit, even when pypdf's
    copy grows (review of Step 10)."""
    monkeypatch.setattr(imports_service, "unlock_pdf", oversized_copy)

    response = post_pdf(client, alice, content=locked_pdf, account_id=account_id, password=PASSWORD)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert count(db, StatementUpload) == 0


def test_an_unlocked_copy_over_the_limit_fails_the_waiting_upload(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    locked_pdf: bytes,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload_id = upload_waiting_for_password(
        client, alice, content=locked_pdf, account_id=account_id
    )
    monkeypatch.setattr(imports_service, "unlock_pdf", oversized_copy)

    response = send_password(client, alice, upload_id, PASSWORD)

    assert response.status_code == 413
    upload = upload_row(db, upload_id)
    assert upload.status is UploadStatus.FAILED
    assert upload.error_message == response.json()["error"]["message"]
    assert count(db, UploadBlob) == 0


def test_no_row_lock_is_held_while_the_pdf_is_unlocked(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    locked_pdf: bytes,
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pypdf runs between two transactions, not under the row lock (review
    of Step 10). A second session asks for the row with SKIP LOCKED while
    the unlock runs - exactly what DELETE /uploads/{id} does - and gets it,
    so a delete at that moment is not told "being imported right now"."""
    upload_id = upload_waiting_for_password(
        client, alice, content=locked_pdf, account_id=account_id
    )
    real_unlock = imports_service.unlock_pdf
    row_was_free: list[bool] = []

    def unlock_and_probe(content: bytes, password: str | None = None) -> bytes:
        with Session(migrated_engine) as other:
            found = other.scalar(
                select(StatementUpload.id)
                .where(StatementUpload.id == uuid.UUID(upload_id))
                .with_for_update(skip_locked=True)
            )
            row_was_free.append(found is not None)
        return real_unlock(content, password)

    monkeypatch.setattr(imports_service, "unlock_pdf", unlock_and_probe)

    assert send_password(client, alice, upload_id, PASSWORD).status_code == 202
    assert row_was_free == [True]


@pytest.mark.parametrize(
    ("change", "status_code"),
    [("deleted", 404), ("unlocked_by_another_request", 409)],
)
def test_a_change_during_the_unlock_is_noticed(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    locked_pdf: bytes,
    db: Session,
    migrated_engine: Engine,
    held_queue: Queue,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    status_code: int,
) -> None:
    """With no lock held while pypdf works, the store step checks again
    under the lock: the upload must still exist and still be waiting.
    Whatever changed meanwhile wins, and nothing is stored or queued."""
    upload_id = upload_waiting_for_password(
        client, alice, content=locked_pdf, account_id=account_id
    )
    real_unlock = imports_service.unlock_pdf
    row = StatementUpload.id == uuid.UUID(upload_id)

    def unlock_while_it_changes(content: bytes, password: str | None = None) -> bytes:
        with Session(migrated_engine) as other:
            if change == "deleted":
                other.execute(delete(StatementUpload).where(row))
            else:
                other.execute(update(StatementUpload).where(row).values(status="pending"))
            other.commit()
        return real_unlock(content, password)

    monkeypatch.setattr(imports_service, "unlock_pdf", unlock_while_it_changes)

    response = send_password(client, alice, upload_id, PASSWORD)

    assert response.status_code == status_code
    assert held_queue.get_jobs() == []
    if change == "unlocked_by_another_request":
        assert response.json()["error"]["message"] == imports_service.NOT_LOCKED_MESSAGE
        # The other request's file stands; this one did not overwrite it.
        assert stored_file(db, upload_id) == locked_pdf


def test_a_simultaneous_upload_of_a_locked_pdf_still_asks_for_the_password(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    locked_pdf: bytes,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The race the unique index catches (a double-clicked upload): the
    request that loses must still answer PASSWORD_REQUIRED with the id the
    password goes to, not a bare "already uploaded" (review of Step 10).

    Simulated like test_uploads.py does: the "already uploaded?" query
    misses once, so the second upload reaches the INSERT."""
    upload_id = upload_waiting_for_password(
        client, alice, content=locked_pdf, account_id=account_id
    )
    real_lookup = imports_service._live_upload
    lookups: list[object] = []

    def misses_the_first_time(*args: Any) -> StatementUpload | None:
        lookups.append(args)
        return None if len(lookups) == 1 else real_lookup(*args)

    monkeypatch.setattr(imports_service, "_live_upload", misses_the_first_time)

    second = post_pdf(client, alice, content=locked_pdf, account_id=account_id)

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "PASSWORD_REQUIRED"
    assert second.json()["error"]["details"] == {"upload_id": upload_id}
    assert len(lookups) == 2
    assert count(db, StatementUpload) == 1


# --- CLAUDE.md §7.2: never persisted -------------------------------------------------


def everything_in_redis(redis: Redis) -> bytes:
    """Every value of every key in Redis, as one blob of bytes.

    RQ compresses a job's pickled call with zlib (its `data` field), so any
    value that is zlib data is searched decompressed as well.
    """
    found: list[bytes] = []
    for key in redis.scan_iter():
        kind = redis.type(key)
        if kind == b"hash":
            values = list(redis.hgetall(key).values())
        elif kind == b"string":
            values = [redis.get(key)]
        elif kind == b"list":
            values = redis.lrange(key, 0, -1)
        elif kind == b"set":
            values = list(redis.smembers(key))
        elif kind == b"zset":
            values = redis.zrange(key, 0, -1)
        else:
            values = []
        for value in values:
            found.append(value)
            try:
                found.append(zlib.decompress(value))
            except zlib.error:
                pass
    return b"\n".join(found)


def everything_in_the_database(db: Session) -> str:
    """Every row of every application table, as text (bytea as its bytes' repr)."""
    db.expire_all()
    rows: list[str] = []
    for table in Base.metadata.sorted_tables:
        rows.extend(str(tuple(row)) for row in db.execute(select(table)))
    return "\n".join(rows)


def test_the_password_is_never_stored_logged_or_echoed(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    locked_pdf: bytes,
    db: Session,
    held_queue: Queue,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CLAUDE.md §7.2, the whole flow: no password, a wrong one, the right one.

    Searched for both passwords afterwards: every response body, every
    structlog event (as raw dicts - before any redaction, so a leak cannot
    hide behind the redactor), every stdlib log record, every row of every
    table, the stored file, and every value in Redis, the job included.
    """
    caplog.set_level(logging.DEBUG)
    with structlog.testing.capture_logs() as events:
        upload_id = upload_waiting_for_password(
            client, alice, content=locked_pdf, account_id=account_id
        )
        wrong = send_password(client, alice, upload_id, WRONG_PASSWORD)
        right = send_password(client, alice, upload_id, PASSWORD)
    assert (wrong.status_code, right.status_code) == (400, 202)

    [job] = held_queue.get_jobs()
    assert job.args == (upload_id,)
    assert job.kwargs == {}

    stored = stored_file(db, upload_id)
    database = everything_in_the_database(db)
    redis = everything_in_redis(get_redis())
    for secret in (PASSWORD, WRONG_PASSWORD):
        assert secret not in wrong.text + right.text
        assert secret not in str(events)
        assert secret not in caplog.text
        assert secret not in database
        assert secret.encode() not in stored
        assert secret.encode() not in redis

    # And after the import itself (the worker side).
    with structlog.testing.capture_logs() as worker_events:
        jobs.process_statement(upload_id)
    assert upload_row(db, upload_id).status is UploadStatus.COMPLETED
    assert PASSWORD not in str(worker_events)
    assert PASSWORD not in everything_in_the_database(db)
