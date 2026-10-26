"""The functions the RQ worker runs (SPEC §11).

Every job follows the same three rules from the SPEC:

1. **It opens its own database session.** A job runs in the worker
   process, long after the request that enqueued it has finished, so
   there is no request session to borrow.
2. **It is safe to run again** (idempotent). RQ retries a job after an
   unexpected error, and a job can be enqueued twice by mistake. Running
   `process_statement` on an upload that is already finished does nothing
   (see `process_upload`).
3. **On final failure it marks the upload `failed`** with a message the
   user can read, so a client polling `GET /uploads/{id}` is never left
   looking at `processing` forever.

The work itself is in `app/services/imports.py`; a job is only the thin
wrapper that gives the service a session, the same way a route is a thin
wrapper that gives it a request.
"""

import uuid
from types import TracebackType

import structlog
from redis import Redis
from rq.job import Job

from app.db.session import SessionLocal
from app.services import imports as imports_service

log = structlog.get_logger()


def process_statement(upload_id: str) -> None:
    """Import one uploaded statement: parse it and store its transactions.

    `upload_id` is the job's ONLY argument (SPEC §11). It arrives as the
    string `enqueue_statement_processing` put in the job.

    An expected failure (a file we cannot read, the wrong account) is
    recorded on the upload by the service and the job finishes normally:
    retrying would give the same answer. Anything unexpected is raised, so
    that RQ retries it (`Retry(max=3)` in app/workers/queue.py).
    """
    # `with` closes the session at the end, even if the service raises;
    # closing rolls back anything that was not committed.
    with SessionLocal() as db:
        imports_service.process_upload(db, uuid.UUID(upload_id))


def on_process_statement_failure(
    job: Job,
    connection: Redis,
    exc_type: type[BaseException],
    exc_value: BaseException,
    traceback: TracebackType,
) -> None:
    """RQ's `on_failure` callback for `process_statement`.

    The arguments are fixed by RQ: the job, its Redis connection, and the
    exception as `sys.exc_info()` gives it.

    **RQ calls this after EVERY failed attempt, including the ones it is
    about to retry** (rq/worker/base.py runs the callback before it decides
    whether to retry). So the first thing to check is whether a retry is
    coming. `job.should_retry` is true while `retries_left > 0`; only when
    it is false is this failure the last one, and only then is the upload
    marked `failed`.

    The exception is deliberately NOT written into `error_message`: it is
    an internal error, its text means nothing to the user, and it could
    contain internals. RQ keeps the traceback in its failed-job registry
    for a week (`FAILURE_TTL`), which is where to look.
    """
    upload_id = uuid.UUID(job.args[0])
    if job.should_retry:
        log.warning(
            "statement_import_will_retry",
            upload_id=str(upload_id),
            retries_left=job.retries_left,
            error_type=exc_type.__name__,
        )
        return

    with SessionLocal() as db:
        imports_service.fail_upload(
            db,
            upload_id,
            imports_service.UNEXPECTED_FAILURE_MESSAGE,
            reason="unexpected_error",
        )
