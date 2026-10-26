"""The worker plumbing, without Redis or Postgres.

What runs inside a job is tested against the real services in
tests/integration/test_upload_worker.py. Here: the worker's entry point,
the queue dependency, the job names the API enqueues by, and the one
branch of the failure callback that must not touch the database at all.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from redis import Redis
from rq import Queue
from rq.utils import import_attribute

from app.core.errors import InvalidStatementError
from app.core.redis import get_redis
from app.parsers.generic_csv import ColumnMapping
from app.services import imports as imports_service
from app.workers import jobs, worker
from app.workers.queue import (
    ON_PROCESS_STATEMENT_FAILURE,
    ON_PROCESS_STATEMENT_STOPPED,
    PROCESS_STATEMENT,
    QUEUE_NAME,
    get_queue,
)


class FakeWorker:
    """Records what `main()` builds and calls, instead of processing jobs."""

    created: list["FakeWorker"] = []

    def __init__(self, queues: list[str], connection: Redis, **options: Any) -> None:
        self.queues = queues
        self.connection = connection
        self.options = options
        self.work_kwargs: dict[str, Any] | None = None
        FakeWorker.created.append(self)

    def work(self, **kwargs: Any) -> None:
        self.work_kwargs = kwargs


def test_the_worker_listens_on_the_queue_the_api_uses(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main()` must listen on QUEUE_NAME and run RQ's scheduler, which is
    what moves a delayed retry back onto the queue."""
    FakeWorker.created.clear()
    monkeypatch.setattr(worker, "Worker", FakeWorker)

    worker.main()

    [built] = FakeWorker.created
    assert built.queues == [QUEUE_NAME]
    assert built.work_kwargs == {"with_scheduler": True}
    # A job whose process DIES never reaches on_failure; this handler is
    # how such an upload still ends up `failed`.
    assert built.options == {"work_horse_killed_handler": jobs.on_work_horse_killed}


def test_the_worker_has_its_own_redis_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """RQ raises the socket timeout of the client it is given, so the
    worker must not be handed the API's shared client (2 s timeouts)."""
    FakeWorker.created.clear()
    monkeypatch.setattr(worker, "Worker", FakeWorker)

    worker.main()

    [built] = FakeWorker.created
    assert built.connection is not get_redis()
    # Same server as the API, though: both come from REDIS_URL.
    ours = built.connection.connection_pool.connection_kwargs
    shared = get_redis().connection_pool.connection_kwargs
    assert (ours["host"], ours["port"]) == (shared["host"], shared["port"])


def test_the_queue_dependency_uses_the_shared_redis_client() -> None:
    """The API side keeps the shared client and its short timeouts, so an
    upload fails fast with a 503 when Redis is down. No connection is made
    by building a Queue."""
    queue = get_queue()

    assert isinstance(queue, Queue)
    assert queue.name == QUEUE_NAME
    assert queue.connection is get_redis()


def test_the_job_names_resolve_to_the_job_functions() -> None:
    """The API enqueues by dotted name, so a renamed function would only
    fail in the worker, at run time. This catches it in the test run."""
    assert import_attribute(PROCESS_STATEMENT) is jobs.process_statement
    assert import_attribute(ON_PROCESS_STATEMENT_FAILURE) is jobs.on_process_statement_failure
    assert import_attribute(ON_PROCESS_STATEMENT_STOPPED) is jobs.on_process_statement_stopped


def test_a_failure_that_will_be_retried_touches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """RQ calls the callback before EVERY retry. While retries are left,
    the callback must not open the database, let alone fail the upload."""

    def no_database() -> None:
        raise AssertionError("the callback must not open a session before the last attempt")

    monkeypatch.setattr(jobs, "CallbackSessionLocal", no_database)
    job = SimpleNamespace(
        args=(str(uuid.uuid4()),), func_name=PROCESS_STATEMENT, should_retry=True, retries_left=2
    )

    jobs.on_process_statement_failure(job, get_redis(), RuntimeError, RuntimeError("x"), None)
    jobs.on_work_horse_killed(job, 1234, 9, None)


def test_the_killed_horse_handler_ignores_other_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """It is registered on the whole worker, so it also sees the jobs of
    later steps (cleanup, analysis). Those have no upload to fail."""

    def no_database() -> None:
        raise AssertionError("another job type must not touch uploads")

    monkeypatch.setattr(jobs, "CallbackSessionLocal", no_database)
    job = SimpleNamespace(
        args=(), func_name="app.workers.jobs.cleanup", should_retry=False, retries_left=0
    )

    jobs.on_work_horse_killed(job, 1234, 9, None)


def test_a_column_mapping_survives_the_trip_through_the_database() -> None:
    """Stored as JSON by the API, rebuilt by the worker: the same mapping."""
    mapping = ColumnMapping(
        date_column="Date",
        description_column="Narration",
        date_format="%d/%m/%Y",
        amount_column="Amount",
    )

    stored = imports_service._mapping_to_json(mapping)

    assert stored is not None
    assert imports_service._mapping_from_json(stored) == mapping
    assert imports_service._mapping_to_json(None) is None
    assert imports_service._mapping_from_json(None) is None


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("statement.pdf", b"%PDF-1.7\n", imports_service.PDF_NOT_SUPPORTED_MESSAGE),
        ("statement.xls", b"", "file is empty"),
        (
            "notes.docx",
            b"just some notes",
            "unsupported file type; upload a CSV or an .xls statement",
        ),
    ],
)
def test_files_that_are_not_statements_are_refused_before_storing(
    filename: str, content: bytes, message: str
) -> None:
    """Decided from the first bytes alone, while the user is still waiting
    for the response - not after a round trip through the worker."""
    with pytest.raises(InvalidStatementError) as excinfo:
        imports_service._supported_file_type(filename, content)

    assert excinfo.value.message == message
