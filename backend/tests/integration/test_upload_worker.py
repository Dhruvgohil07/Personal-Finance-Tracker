"""The worker side of an upload: the RQ job, its retries and its failures.

test_uploads.py drives uploads through the synchronous test queue, where
the job has finished before POST returns. The tests here need the moment
in between, so most of them use `held_queue`: a real, asynchronous queue
with no worker listening. The job then waits in Redis exactly as it would
in production, the test can look at it, and the test plays the worker by
calling the job function itself.

The rules defended here, and where they come from:

- the job's only argument is the upload id          SPEC §11, CLAUDE.md §7.2
- running a job twice changes nothing                SPEC §11 (idempotent)
- an unexpected error is retried, and only the
  LAST failure marks the upload `failed`             ADR 013
- Redis down -> 503, and no upload is left `pending` Phase 2 plan
"""

import uuid
import zlib
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient
from httpx import Response
from redis import Redis
from rq import Queue
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.redis import get_redis
from app.models import StatementUpload, Transaction, UploadBlob, UploadStatus
from app.services import imports as imports_service
from app.workers import jobs
from app.workers.queue import (
    FAILURE_TTL,
    ON_PROCESS_STATEMENT_FAILURE,
    PROCESS_STATEMENT,
    RETRY_INTERVALS_SECONDS,
    RETRY_MAX,
    get_queue,
)
from tests.fixtures.icici_xls import build_icici_xls

pytestmark = pytest.mark.integration

UPLOADS = "/api/v1/uploads"
ACCOUNTS = "/api/v1/accounts"

AuthHeadersFactory = Callable[[str], dict[str, str]]


# --- Fixtures and helpers ---------------------------------------------------


@pytest.fixture
def alice(make_auth_headers: AuthHeadersFactory) -> dict[str, str]:
    return make_auth_headers("alice@example.com")


@pytest.fixture
def account_id(client: TestClient, alice: dict[str, str]) -> str:
    response = client.post(
        ACCOUNTS,
        json={
            "bank_code": "ICICI",
            "nickname": "Salary account",
            "account_type": "savings",
            "masked_number": "1234",
        },
        headers=alice,
    )
    assert response.status_code == 201
    account: str = response.json()["id"]
    return account


@pytest.fixture
def statement(tmp_path: Path) -> bytes:
    """The default synthetic ICICI statement: five transactions."""
    return build_icici_xls(tmp_path / "statement.xls").read_bytes()


@pytest.fixture
def held_queue(client: TestClient, sync_queue: Queue) -> Iterator[Queue]:
    """A real queue that nobody works on: jobs wait in Redis until run by hand.

    Swapped into the app for this test only, in place of `sync_queue`. It
    has the test queue's name, so the development worker never sees it,
    and it is emptied before and after so no job leaks between tests.
    """
    queue = Queue(sync_queue.name, connection=get_redis())
    queue.empty()
    client.app.dependency_overrides[get_queue] = lambda: queue
    yield queue
    queue.empty()


def post_upload(
    client: TestClient, headers: dict[str, str], *, content: bytes, account_id: str
) -> Response:
    return client.post(
        UPLOADS,
        headers=headers,
        files={"file": ("statement.xls", content, "application/octet-stream")},
        data={"account_id": account_id},
    )


def queue_upload(
    client: TestClient, headers: dict[str, str], *, content: bytes, account_id: str
) -> str:
    """POST a file and return the new upload's id (the job is not run)."""
    response = post_upload(client, headers, content=content, account_id=account_id)
    assert response.status_code == 202
    upload_id: str = response.json()["id"]
    return upload_id


def upload_row(db: Session, upload_id: str) -> StatementUpload:
    """The upload as it is in the database NOW (not a cached copy)."""
    db.expire_all()
    upload = db.get(StatementUpload, uuid.UUID(upload_id))
    assert upload is not None
    return upload


def count(db: Session, model: type[Any]) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def final_failure(upload_id: str) -> Any:
    """Stand-in for an RQ job that has used up its retries.

    `on_process_statement_failure` reads only these three attributes. A
    real `Job` would have to be loaded from Redis and its retry counter
    edited, which tests RQ more than our callback.
    """
    return SimpleNamespace(args=(upload_id,), should_retry=False, retries_left=0)


# --- The job in Redis -------------------------------------------------------


def test_the_job_carries_only_the_upload_id(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    statement: bytes,
    held_queue: Queue,
) -> None:
    """SPEC §11 and CLAUDE.md §7.2: job arguments are `(upload_id,)` only.

    Job arguments are pickled into Redis unencrypted, so whatever is put
    there - a filename, file bytes, one day a statement password - can be
    read by anyone who can read Redis. The check is on the job object AND
    on the raw bytes RQ stored, because the second is what would leak.
    """
    upload_id = queue_upload(client, alice, content=statement, account_id=account_id)

    [job] = held_queue.get_jobs()
    assert job.func_name == PROCESS_STATEMENT
    assert job.args == (upload_id,)
    assert job.kwargs == {}

    stored = get_redis().hgetall(job.key)
    # `data` holds the pickled call, compressed with zlib by RQ.
    raw = b" ".join(stored.values()) + zlib.decompress(stored[b"data"])
    for secret in (b"statement.xls", b"TESTSHOP", b"alice@example.com", b"084600001234"):
        assert secret not in raw, f"{secret!r} must not be stored in the job"


def test_the_job_is_enqueued_with_retries_and_a_failure_callback(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    statement: bytes,
    held_queue: Queue,
) -> None:
    """The options that make a crash end in `failed` instead of `processing`.

    Read back from the job RQ stored, so this checks what the worker will
    actually see, not what the code meant to send.
    """
    queue_upload(client, alice, content=statement, account_id=account_id)

    [job] = held_queue.get_jobs()
    assert job.retries_left == RETRY_MAX
    assert job.retry_intervals == RETRY_INTERVALS_SECONDS
    assert job.failure_callback is jobs.on_process_statement_failure
    assert job.failure_ttl == FAILURE_TTL.total_seconds()
    # The name is what is stored; it must resolve to the real function.
    assert job.func is jobs.process_statement
    assert ON_PROCESS_STATEMENT_FAILURE == "app.workers.jobs.on_process_statement_failure"


# --- Running the job --------------------------------------------------------


def test_an_upload_waits_as_pending_until_the_job_runs(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    statement: bytes,
    held_queue: Queue,
    db: Session,
) -> None:
    upload_id = queue_upload(client, alice, content=statement, account_id=account_id)

    # Before the worker: the row and the encrypted file are both there.
    assert upload_row(db, upload_id).status is UploadStatus.PENDING
    assert db.get(UploadBlob, uuid.UUID(upload_id)) is not None
    assert count(db, Transaction) == 0

    jobs.process_statement(upload_id)

    upload = upload_row(db, upload_id)
    assert upload.status is UploadStatus.COMPLETED
    assert upload.rows_inserted == 5
    assert upload.completed_at is not None
    assert db.get(UploadBlob, uuid.UUID(upload_id)) is None
    assert count(db, Transaction) == 5


def test_running_the_job_again_changes_nothing(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    statement: bytes,
    held_queue: Queue,
    db: Session,
) -> None:
    """Idempotent (SPEC §11): a second run of a finished job is a no-op."""
    upload_id = queue_upload(client, alice, content=statement, account_id=account_id)
    jobs.process_statement(upload_id)
    first_completed_at = upload_row(db, upload_id).completed_at

    with structlog.testing.capture_logs() as events:
        jobs.process_statement(upload_id)

    upload = upload_row(db, upload_id)
    assert upload.status is UploadStatus.COMPLETED
    assert upload.completed_at == first_completed_at
    assert (upload.rows_parsed, upload.rows_inserted, upload.rows_duplicate) == (5, 5, 0)
    assert count(db, Transaction) == 5
    assert [event["event"] for event in events] == ["statement_import_skipped"]


def test_a_job_for_a_deleted_upload_does_nothing(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    statement: bytes,
    held_queue: Queue,
    db: Session,
) -> None:
    """The user deleted the upload while its job was still in the queue."""
    upload_id = queue_upload(client, alice, content=statement, account_id=account_id)
    assert client.delete(f"{UPLOADS}/{upload_id}", headers=alice).status_code == 204

    jobs.process_statement(upload_id)

    assert count(db, StatementUpload) == 0
    assert count(db, Transaction) == 0


def test_an_attempt_that_crashed_is_finished_by_the_retry(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    statement: bytes,
    held_queue: Queue,
    db: Session,
) -> None:
    """A worker that died half-way leaves the row `processing` (its import
    transaction rolled back). RQ's retry must pick that up, not skip it."""
    upload_id = queue_upload(client, alice, content=statement, account_id=account_id)
    upload = upload_row(db, upload_id)
    upload.status = UploadStatus.PROCESSING
    db.commit()

    jobs.process_statement(upload_id)

    assert upload_row(db, upload_id).status is UploadStatus.COMPLETED
    assert count(db, Transaction) == 5


def test_the_import_transaction_rechecks_the_status_after_locking(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    statement: bytes,
    held_queue: Queue,
    db: Session,
    job_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second transaction must not trust what the first one saw.

    The real case is a job enqueued twice: both runs claim the upload, one
    wins the row lock and completes it, and the other then finds a
    finished upload when it finally gets the lock. Two real concurrent
    workers cannot be staged here, so the claim is stubbed to say "go on"
    without setting `processing` - which is what the second run sees: a
    status that is no longer `processing`. It must stop, not import.
    """
    upload_id = queue_upload(client, alice, content=statement, account_id=account_id)
    monkeypatch.setattr(imports_service, "_claim", lambda *_args: True)

    with job_sessions() as session:
        imports_service.process_upload(session, uuid.UUID(upload_id))

    assert upload_row(db, upload_id).status is UploadStatus.PENDING
    assert count(db, Transaction) == 0


# --- Expected failures: recorded at once, never retried ---------------------


def test_a_missing_file_fails_the_upload(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    statement: bytes,
    held_queue: Queue,
    db: Session,
) -> None:
    """The stored file is gone (in practice: the Step 3 cleanup removed a
    blob that waited in the queue for over 24 hours)."""
    upload_id = queue_upload(client, alice, content=statement, account_id=account_id)
    db.delete(db.get_one(UploadBlob, uuid.UUID(upload_id)))
    db.commit()

    jobs.process_statement(upload_id)

    upload = upload_row(db, upload_id)
    assert upload.status is UploadStatus.FAILED
    assert upload.error_message == imports_service.FILE_MISSING_MESSAGE


def test_a_damaged_file_fails_the_upload(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    statement: bytes,
    held_queue: Queue,
    db: Session,
) -> None:
    """One flipped byte: GCM's authentication tag no longer matches, so the
    file is refused instead of being parsed as garbage (ADR 012)."""
    upload_id = queue_upload(client, alice, content=statement, account_id=account_id)
    blob = db.get_one(UploadBlob, uuid.UUID(upload_id))
    damaged = bytearray(blob.encrypted_content)
    damaged[0] ^= 0xFF
    blob.encrypted_content = bytes(damaged)
    db.commit()

    jobs.process_statement(upload_id)

    upload = upload_row(db, upload_id)
    assert upload.status is UploadStatus.FAILED
    assert upload.error_message == imports_service.FILE_UNREADABLE_MESSAGE
    assert db.get(UploadBlob, uuid.UUID(upload_id)) is None


# --- Unexpected failures: retried by RQ, failed at the end ------------------


def explode(*_args: Any) -> None:
    """Stands in for a database blip in the middle of an import."""
    raise RuntimeError("connection lost")


def test_an_unexpected_error_leaves_the_upload_for_the_retry(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    statement: bytes,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The job raises; RQ will retry it, so nothing may be marked final yet.

    The upload stays `processing` with its file still stored, and no
    transactions were committed - exactly the state the retry needs. RQ
    calls the failure callback even now, before retrying, and the callback
    must recognise that a retry is coming.

    (The synchronous test queue never actually retries; it calls the
    callback once. That one call is the case under test.)
    """
    monkeypatch.setattr(imports_service, "store_transactions", explode)

    with structlog.testing.capture_logs() as events:
        response = post_upload(client, alice, content=statement, account_id=account_id)

    assert response.status_code == 202
    upload = upload_row(db, response.json()["id"])
    assert upload.status is UploadStatus.PROCESSING
    assert db.get(UploadBlob, upload.id) is not None
    assert count(db, Transaction) == 0
    [retry] = [event for event in events if event["event"] == "statement_import_will_retry"]
    assert retry["retries_left"] == RETRY_MAX
    assert retry["error_type"] == "RuntimeError"


def test_the_last_failure_marks_the_upload_failed(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    statement: bytes,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retries used up: `failed`, a message for the user, the file deleted.

    The exception text ("connection lost") is internal and must not reach
    `error_message`. And `failed` frees the file for a new upload.
    """
    monkeypatch.setattr(imports_service, "store_transactions", explode)
    upload_id = post_upload(client, alice, content=statement, account_id=account_id).json()["id"]

    jobs.on_process_statement_failure(
        final_failure(upload_id), get_redis(), RuntimeError, RuntimeError("connection lost"), None
    )

    upload = upload_row(db, upload_id)
    assert upload.status is UploadStatus.FAILED
    assert upload.error_message == imports_service.UNEXPECTED_FAILURE_MESSAGE
    assert "connection lost" not in upload.error_message
    assert db.get(UploadBlob, upload.id) is None
    # Accepted again (not 409). Whether the new attempt then succeeds is
    # not the point; `store_transactions` is still broken in this test.
    again = post_upload(client, alice, content=statement, account_id=account_id)
    assert again.status_code == 202


def test_a_late_failure_callback_does_not_undo_a_finished_import(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    statement: bytes,
    db: Session,
) -> None:
    """`fail_upload` only touches unfinished uploads: a `completed` import
    stays completed whatever arrives afterwards."""
    upload_id = post_upload(client, alice, content=statement, account_id=account_id).json()["id"]
    assert upload_row(db, upload_id).status is UploadStatus.COMPLETED

    jobs.on_process_statement_failure(
        final_failure(upload_id), get_redis(), RuntimeError, RuntimeError("late"), None
    )

    upload = upload_row(db, upload_id)
    assert upload.status is UploadStatus.COMPLETED
    assert upload.error_message is None


# --- Redis unavailable ------------------------------------------------------


def test_redis_down_is_a_503_and_the_upload_is_failed(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    statement: bytes,
    db: Session,
) -> None:
    """The row and file are committed before the enqueue, so a failed
    enqueue would leave a `pending` upload that no job will ever process.
    It is failed instead, its file deleted, and the client gets a 503.

    Port 1 on localhost: nothing listens there, so connecting is refused
    at once - a real Redis connection error, not a mock.
    """
    unreachable = Queue(
        "unreachable", connection=Redis(host="127.0.0.1", port=1, socket_connect_timeout=1)
    )
    client.app.dependency_overrides[get_queue] = lambda: unreachable

    response = post_upload(client, alice, content=statement, account_id=account_id)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    [upload] = db.scalars(select(StatementUpload)).all()
    assert upload.status is UploadStatus.FAILED
    assert upload.error_message == imports_service.QUEUE_UNAVAILABLE_MESSAGE
    assert count(db, UploadBlob) == 0
