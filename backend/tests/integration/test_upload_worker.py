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
- a job whose process died or was stopped still
  ends in `failed`                                   ADR 013
- an import and a delete of its account do not
  deadlock                                           ADR 013 (lock order)
"""

import threading
import time
import uuid
import zlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient
from httpx import Response
from redis import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError
from rq import Queue
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.redis import get_redis
from app.models import StatementUpload, Transaction, UploadBlob, UploadStatus
from app.services import cleanup as cleanup_service
from app.services import imports as imports_service
from app.workers import jobs
from app.workers.queue import (
    FAILURE_TTL,
    ON_PROCESS_STATEMENT_FAILURE,
    PROCESS_STATEMENT,
    RETRY_INTERVALS_SECONDS,
    RETRY_MAX,
    get_queue,
    process_statement_job_id,
    statement_job_waiting_check,
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

    The failure hooks read only these attributes. A real `Job` would have
    to be loaded from Redis and its retry counter edited, which tests RQ
    more than our hooks.
    """
    return SimpleNamespace(
        args=(upload_id,), func_name=PROCESS_STATEMENT, should_retry=False, retries_left=0
    )


def retrying(upload_id: str) -> Any:
    """Stand-in for an RQ job that RQ is about to run again."""
    return SimpleNamespace(
        args=(upload_id,), func_name=PROCESS_STATEMENT, should_retry=True, retries_left=2
    )


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
    # A fixed id, so the stale-upload rule can find the job from the upload.
    assert job.id == process_statement_job_id(uuid.UUID(upload_id))
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
    assert job.stopped_callback is jobs.on_process_statement_stopped
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
    # "Go on", handing back the row as it is: still `pending`.
    monkeypatch.setattr(
        imports_service,
        "_claim",
        lambda session, upload_id: session.get(StatementUpload, upload_id),
    )

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


# --- The other ways a job can end -------------------------------------------


def test_a_stopped_job_fails_its_upload(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    statement: bytes,
    held_queue: Queue,
    db: Session,
) -> None:
    """RQ never retries a stopped job and does not call on_failure for it,
    so on_stopped is the only chance to mark the upload `failed`."""
    upload_id = queue_upload(client, alice, content=statement, account_id=account_id)

    jobs.on_process_statement_stopped(final_failure(upload_id), get_redis())

    upload = upload_row(db, upload_id)
    assert upload.status is UploadStatus.FAILED
    assert upload.error_message == imports_service.UNEXPECTED_FAILURE_MESSAGE
    assert db.get(UploadBlob, upload.id) is None


def test_a_job_whose_process_died_fails_only_after_its_last_attempt(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    statement: bytes,
    held_queue: Queue,
    db: Session,
) -> None:
    """The child process running the job died (e.g. killed for using too
    much memory). RQ calls the worker's killed-horse handler, NOT the job's
    on_failure. While a retry is coming, nothing changes; after the last
    attempt, the upload is failed."""
    upload_id = queue_upload(client, alice, content=statement, account_id=account_id)

    jobs.on_work_horse_killed(retrying(upload_id), 1234, 9, None)
    assert upload_row(db, upload_id).status is UploadStatus.PENDING

    jobs.on_work_horse_killed(final_failure(upload_id), 1234, 9, None)
    upload = upload_row(db, upload_id)
    assert upload.status is UploadStatus.FAILED
    assert upload.error_message == imports_service.UNEXPECTED_FAILURE_MESSAGE


# --- An enqueue error that arrives after the job ran -------------------------


def test_an_enqueue_error_after_the_job_got_through_is_not_a_503(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    statement: bytes,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis accepted the job, but its reply timed out. The synchronous test
    queue runs the job inside the real enqueue, which stands in for a fast
    worker; then the timeout is raised. The import succeeded, so the user
    must not be told to upload again (they would then get a 409)."""
    real_enqueue = imports_service.enqueue_statement_processing

    def enqueue_then_time_out(queue: Queue, upload_id: uuid.UUID) -> None:
        real_enqueue(queue, upload_id)
        raise RedisTimeoutError("Timeout reading from socket")

    monkeypatch.setattr(imports_service, "enqueue_statement_processing", enqueue_then_time_out)

    response = post_upload(client, alice, content=statement, account_id=account_id)

    assert response.status_code == 202
    assert response.json()["status"] == "completed"
    assert upload_row(db, response.json()["id"]).status is UploadStatus.COMPLETED
    assert count(db, Transaction) == 5


# --- Unfinished uploads of the same file -------------------------------------


def test_a_recent_unfinished_upload_of_the_file_is_a_409_naming_it(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    statement: bytes,
    held_queue: Queue,
) -> None:
    """The first upload is still waiting for the worker. "You have already
    uploaded this file" would be false - nothing is imported yet - so the
    message says it is being imported, and names the upload to poll."""
    upload_id = queue_upload(client, alice, content=statement, account_id=account_id)

    again = post_upload(client, alice, content=statement, account_id=account_id)

    assert again.status_code == 409
    error = again.json()["error"]
    assert error["message"] == imports_service.FILE_BEING_IMPORTED_MESSAGE
    assert error["details"] == {"upload_id": upload_id}


def test_a_stuck_upload_of_the_file_is_failed_and_the_new_one_accepted(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    statement: bytes,
    held_queue: Queue,
    db: Session,
) -> None:
    """An upload `pending` for longer than STALE_UPLOAD_AFTER, whose job is
    gone from Redis (lost between commit and enqueue), will never finish.
    It must not block the file until a cleanup job runs."""
    stuck_id = queue_upload(client, alice, content=statement, account_id=account_id)
    make_old(db, stuck_id)
    held_queue.empty()  # the job is lost

    again = post_upload(client, alice, content=statement, account_id=account_id)

    assert again.status_code == 202
    assert again.json()["id"] != stuck_id
    stuck = upload_row(db, stuck_id)
    assert stuck.status is UploadStatus.FAILED
    assert stuck.error_message == imports_service.STALE_UPLOAD_MESSAGE
    assert db.get(UploadBlob, stuck.id) is None


def make_old(db: Session, upload_id: str) -> None:
    """Pretend the upload was created just over STALE_UPLOAD_AFTER ago."""
    upload = upload_row(db, upload_id)
    upload.created_at = (
        datetime.now(UTC) - imports_service.STALE_UPLOAD_AFTER - timedelta(minutes=1)
    )
    db.commit()


def test_an_old_upload_whose_job_still_waits_is_a_409_not_failed(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    statement: bytes,
    held_queue: Queue,
    db: Session,
) -> None:
    """Over an hour old, but its job is still queued (the worker was down).
    It is about to import, so it is not failed: the new upload is refused
    as "being imported", naming the old one."""
    old_id = queue_upload(client, alice, content=statement, account_id=account_id)
    make_old(db, old_id)

    again = post_upload(client, alice, content=statement, account_id=account_id)

    assert again.status_code == 409
    assert again.json()["error"]["message"] == imports_service.FILE_BEING_IMPORTED_MESSAGE
    assert upload_row(db, old_id).status is UploadStatus.PENDING


def test_a_stuck_upload_failed_by_the_cleanup_meanwhile_still_frees_the_file(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    statement: bytes,
    held_queue: Queue,
    db: Session,
    job_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The race from the Step 3 review. The request has read the stuck
    upload as `pending`; the hourly cleanup fails it just before the
    request's own attempt. The request's attempt then matches nothing - and
    it must still see that the file is free, not answer "already uploaded".
    """
    stuck_id = queue_upload(client, alice, content=statement, account_id=account_id)
    make_old(db, stuck_id)
    held_queue.empty()
    real_fail_stale = imports_service.fail_stale_uploads

    def cleanup_gets_there_first(*args: Any, **kwargs: Any) -> Any:
        with job_sessions() as other:
            cleanup_service.run_cleanup(
                other, now=datetime.now(UTC), job_is_waiting=lambda _upload_id: False
            )
        return real_fail_stale(*args, **kwargs)

    monkeypatch.setattr(imports_service, "fail_stale_uploads", cleanup_gets_there_first)

    again = post_upload(client, alice, content=statement, account_id=account_id)

    assert again.status_code == 202
    assert upload_row(db, stuck_id).status is UploadStatus.FAILED


# --- Is an upload's job still waiting? ----------------------------------------


def test_the_waiting_check_reads_the_jobs_state_in_redis(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    statement: bytes,
    held_queue: Queue,
) -> None:
    upload_id = uuid.UUID(queue_upload(client, alice, content=statement, account_id=account_id))
    job_is_waiting = statement_job_waiting_check(get_redis())

    # Queued, nobody has run it yet.
    assert job_is_waiting(upload_id) is True
    # Gone from Redis (a lost job). A FINISHED job is the next test.
    held_queue.empty()
    assert job_is_waiting(upload_id) is False
    # An upload that never had a job.
    assert job_is_waiting(uuid.uuid4()) is False


def test_the_waiting_check_assumes_waiting_when_redis_is_down() -> None:
    """Better to fail nothing this hour than to fail an upload whose job
    may well be queued: the next run decides."""
    unreachable = Redis(host="127.0.0.1", port=1, socket_connect_timeout=1)

    assert statement_job_waiting_check(unreachable)(uuid.uuid4()) is True


def test_a_job_that_ran_is_no_longer_waiting(
    client: TestClient, alice: dict[str, str], account_id: str, statement: bytes
) -> None:
    """On the synchronous test queue the job has finished by the time POST
    returns: its state in Redis is `finished`."""
    upload_id = post_upload(client, alice, content=statement, account_id=account_id).json()["id"]

    assert statement_job_waiting_check(get_redis())(uuid.UUID(upload_id)) is False


# --- Lock order ---------------------------------------------------------------


def wait_until_a_query_waits_for_a_lock(engine: Engine, timeout: float = 5.0) -> None:
    """Block until some connection to the test database is waiting on a lock."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            waiting = conn.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
        if waiting:
            return
        time.sleep(0.05)
    raise AssertionError("the concurrent DELETE never started waiting for a lock")


def test_deleting_the_account_during_an_import_waits_instead_of_deadlocking(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    statement: bytes,
    held_queue: Queue,
    db: Session,
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The import and a DELETE of its account, at the same moment.

    The DELETE locks the account row and then, by cascade, the upload row.
    The import used to lock the upload row first and the account row
    (through the transactions' foreign key) second: opposite orders, so
    each waited for the other and Postgres aborted one of them. Now the
    import takes the account first, so the DELETE simply waits for it.

    The DELETE is started from a second thread in the middle of the import
    (just before the INSERT), and the import only goes on once Postgres
    reports the DELETE as waiting for a lock. Before the fix, this test
    ends in a DeadlockDetected error on one side.
    """
    upload_id = queue_upload(client, alice, content=statement, account_id=account_id)
    outcome: dict[str, Any] = {}

    def delete_the_account() -> None:
        try:
            with migrated_engine.begin() as conn:
                conn.execute(text("DELETE FROM accounts WHERE id = :id"), {"id": account_id})
            outcome["deleted"] = True
        except Exception as exc:  # reported by the assertion below
            outcome["error"] = exc

    deleter = threading.Thread(target=delete_the_account)
    real_store = imports_service.store_transactions

    def store_while_the_account_is_deleted(*args: Any) -> Any:
        deleter.start()
        wait_until_a_query_waits_for_a_lock(migrated_engine)
        return real_store(*args)

    monkeypatch.setattr(imports_service, "store_transactions", store_while_the_account_is_deleted)

    jobs.process_statement(upload_id)
    deleter.join(timeout=10)

    assert outcome == {"deleted": True}
    # The import committed first, then the DELETE removed it all by cascade.
    assert count(db, StatementUpload) == 0
    assert count(db, Transaction) == 0
