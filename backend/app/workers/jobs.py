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

**Rule 3 needs three hooks, not one**, because RQ reports a job's end in
three different ways (read in RQ 2.12's source):

- the job raised an exception -> `on_failure` (`on_process_statement_failure`)
  - also used by RQ's cleanup for a job whose worker vanished ("abandoned");
- the job was stopped on purpose (`rq` stop command) -> `on_stopped`
  (`on_process_statement_stopped`). A stopped job is never retried;
- the child process running the job died (out of memory, a crash in a C
  library, killed after its timeout) -> the worker's
  `work_horse_killed_handler` (`on_work_horse_killed`). RQ does NOT call
  `on_failure` in this case.

All three end in `_fail_after_last_attempt`.
"""

import uuid
from datetime import UTC, datetime
from types import TracebackType

import structlog
from redis import Redis
from rq.job import Job
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.redis import get_redis
from app.db.session import SessionLocal, build_unpooled_engine
from app.services import cleanup as cleanup_service
from app.services import imports as imports_service
from app.workers.queue import PROCESS_STATEMENT, statement_job_waiting_check

log = structlog.get_logger()

# Sessions for the failure hooks. Two of them (the killed-horse handler and
# RQ's abandoned-job cleanup) run in the worker's PARENT process, which
# forks a child for every job, so they must not leave a pooled connection
# behind to be inherited (see `build_unpooled_engine`). The engine is
# created here, but it connects only when a hook actually runs.
CallbackSessionLocal = sessionmaker(
    bind=build_unpooled_engine(get_settings().database_url), expire_on_commit=False
)


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


def cleanup() -> None:
    """The hourly cleanup (SPEC §11). Enqueued by app/workers/scheduler.py.

    Takes no arguments: what to clean up is decided by the database, the
    clock and Redis. The time and the Redis lookup are supplied here, so the
    service can be tested with any time and any answer it is given.
    """
    with SessionLocal() as db:
        cleanup_service.run_cleanup(
            db,
            now=datetime.now(UTC),
            job_is_waiting=statement_job_waiting_check(get_redis()),
        )


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
    if _retry_is_coming(job, error_type=exc_type.__name__):
        return
    _fail_after_last_attempt(job, reason="unexpected_error")


def on_process_statement_stopped(job: Job, connection: Redis) -> None:
    """RQ's `on_stopped` callback: someone stopped the job on purpose.

    RQ never retries a stopped job, so this is always the end of it.
    """
    _fail_after_last_attempt(job, reason="stopped")


def on_work_horse_killed(job: Job, retpid: int, ret_val: int, rusage: object) -> None:
    """The worker's `work_horse_killed_handler`: the job's process died.

    Registered on the Worker (app/workers/worker.py), so it is called for
    EVERY job type; only `process_statement` jobs are handled here. RQ
    calls it before deciding whether to retry, with the same
    `should_retry` rule as `on_failure`.

    The arguments are fixed by RQ: the job, the dead child's pid, its exit
    status from `waitpid`, and its resource usage. Only the job is used.
    (`rusage` is typed `object`: its real type, `resource.struct_rusage`,
    exists only on Unix, and this module must import on Windows too.)
    """
    if job.func_name != PROCESS_STATEMENT:
        return
    if _retry_is_coming(job, error_type="WorkHorseKilled"):
        return
    _fail_after_last_attempt(job, reason="worker_died")


def _retry_is_coming(job: Job, *, error_type: str) -> bool:
    """True, and logged, if RQ will run this job again."""
    if not job.should_retry:
        return False
    log.warning(
        "statement_import_will_retry",
        upload_id=job.args[0],
        retries_left=job.retries_left,
        error_type=error_type,
    )
    return True


def _fail_after_last_attempt(job: Job, *, reason: str) -> None:
    """Mark the job's upload `failed` with the generic message.

    `fail_upload` leaves an upload that is already finished alone, so a
    hook that fires late cannot undo a completed import.
    """
    with CallbackSessionLocal() as db:
        imports_service.fail_upload(
            db,
            uuid.UUID(job.args[0]),
            imports_service.UNEXPECTED_FAILURE_MESSAGE,
            reason=reason,
        )
