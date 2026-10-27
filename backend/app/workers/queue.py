"""The RQ queue: where the API leaves work for the worker (SPEC §11).

How RQ works, in one paragraph: `queue.enqueue(func, *args)` writes a
*job* into Redis - the function's dotted name, its arguments (pickled) and
some options - and pushes the job's id onto a Redis list. A separate
worker process (app/workers/worker.py) waits on that list, takes the id,
loads the job and calls the function. The API and the worker never talk to
each other directly; Redis sits between them.

Two rules from the SPEC shape this module:

- **A job's only argument is an id** - the upload's, or for a job about a
  whole user, the user's (SPEC §11, CLAUDE.md §7.2).
  Everything else the worker needs is read from the database. Job
  arguments are stored in Redis in plain pickle, so anything put there -
  a statement password, a filename - would sit in Redis unencrypted.
- **Jobs are named by their dotted path**, as strings, rather than passed
  as function objects. RQ stores a string anyway, and this way the API
  never imports worker code: `app/workers/jobs.py` imports the import
  service, and the import service enqueues through this module, so
  importing `jobs` here would be a circular import. A test checks that the
  strings resolve to the real functions.
"""

import uuid
from collections.abc import Callable
from datetime import timedelta

from redis import Redis
from redis.exceptions import RedisError
from rq import Callback, Queue, Retry
from rq.exceptions import NoSuchJobError
from rq.job import Job, JobStatus

from app.core.redis import get_redis

# One queue for now. The worker listens on the same name.
QUEUE_NAME = "default"

PROCESS_STATEMENT = "app.workers.jobs.process_statement"
ON_PROCESS_STATEMENT_FAILURE = "app.workers.jobs.on_process_statement_failure"
ON_PROCESS_STATEMENT_STOPPED = "app.workers.jobs.on_process_statement_stopped"
CLEANUP = "app.workers.jobs.cleanup"
RECATEGORIZE_USER = "app.workers.jobs.recategorize_user"

# How often, and after how long, an unexpected error is retried: 3 more
# attempts, 10 s, 1 min and 5 min after the failures before them. Meant for
# a database restart or a network blip, which are over in seconds to
# minutes. A file we cannot parse is NOT retried: the job records that
# failure itself and finishes normally, because parsing the same bytes
# again gives the same answer (ADR 013).
RETRY_MAX = 3
RETRY_INTERVALS_SECONDS = [10, 60, 300]

# How long RQ keeps a job that failed for good in its "failed" registry in
# Redis, with the traceback. RQ's default is a year; a week is plenty to
# investigate, and a traceback is not something to keep around for longer.
FAILURE_TTL = timedelta(days=7)

# The cleanup job is enqueued every hour by app/workers/scheduler.py.
CLEANUP_INTERVAL = timedelta(hours=1)

# Job states in which a job will still run (or is running). A job that is
# finished, failed, stopped or gone will not.
_WILL_STILL_RUN = frozenset(
    {JobStatus.QUEUED, JobStatus.SCHEDULED, JobStatus.STARTED, JobStatus.DEFERRED}
)


def process_statement_job_id(upload_id: uuid.UUID) -> str:
    """The RQ job id of an upload's `process_statement` job.

    Fixed, instead of RQ's random one, so that anything holding just the
    upload id - the stale-upload rule - can find the upload's job in Redis.
    One upload is enqueued once, so the id never repeats. (RQ allows only
    letters, digits, "_" and "-" in a job id, hence no ":".)
    """
    return f"process_statement_{upload_id}"


def statement_job_waiting_check(connection: Redis) -> Callable[[uuid.UUID], bool]:
    """A function answering "will this upload's job still run?", for that Redis.

    Used by the stale-upload rule (`fail_stale_uploads`), which must not
    fail an upload whose job is merely waiting in the queue - e.g. after
    the worker was down for a while.

    If Redis cannot be reached, the answer is "yes, assume it will run":
    then nothing is failed this time, and the next hourly run decides. Being
    wrong in that direction costs an hour; the other direction would make
    the user upload a file again for nothing.
    """

    def job_is_waiting(upload_id: uuid.UUID) -> bool:
        try:
            job = Job.fetch(process_statement_job_id(upload_id), connection=connection)
            return job.get_status(refresh=False) in _WILL_STILL_RUN
        except NoSuchJobError:
            return False
        except RedisError:
            return True

    return job_is_waiting


def get_queue() -> Queue:
    """FastAPI dependency: the queue that uploads are processed from.

    Uses the app's shared Redis client, whose 2 s timeouts make an upload
    fail fast (503) when Redis is down instead of hanging the request.

    Tests override this dependency with a queue built with
    `is_async=False`, which runs each job immediately inside `enqueue()`,
    in the test process. RQ's worker needs `fork()`, which Windows lacks,
    so that is how the jobs are tested on Windows and in CI.
    """
    return Queue(QUEUE_NAME, connection=get_redis())


def enqueue_statement_processing(queue: Queue, upload_id: uuid.UUID) -> Job:
    """Put a `process_statement(upload_id)` job on the queue.

    Raises `redis.exceptions.RedisError` when Redis cannot be reached; the
    caller (`create_upload`) turns that into a 503.

    The id is sent as a string: it is the plainest thing to put in a job,
    and it reads as itself in `rq info` and in the worker's log line.
    """
    return queue.enqueue(
        PROCESS_STATEMENT,
        str(upload_id),
        job_id=process_statement_job_id(upload_id),
        retry=Retry(max=RETRY_MAX, interval=RETRY_INTERVALS_SECONDS),
        # Both end in a `failed` upload once no retry is coming. The third
        # way a job can end - its process dying - is handled by the
        # worker, not the job (see app/workers/jobs.py).
        on_failure=Callback(ON_PROCESS_STATEMENT_FAILURE),
        on_stopped=Callback(ON_PROCESS_STATEMENT_STOPPED),
        failure_ttl=int(FAILURE_TTL.total_seconds()),
    )


def enqueue_recategorization(queue: Queue, user_id: uuid.UUID) -> Job:
    """Put a `recategorize_user(user_id)` job on the queue (ADR 017).

    Enqueued after every change to a user's rules. Its only argument is
    the user id, never the rule: a `vpa` rule's pattern is a person's UPI
    id, and job arguments sit in Redis unencrypted (see the module
    docstring). Everything else is read from the database when it runs.

    Raises `redis.exceptions.RedisError` when Redis cannot be reached; the
    caller (`app/services/rules.py`) then runs the same work in the request.

    Retried like an import for a database blip. No failure callback: there
    is no status to mark, and the rows simply keep their previous
    categories until the next rule change runs the job again.
    """
    return queue.enqueue(
        RECATEGORIZE_USER,
        str(user_id),
        retry=Retry(max=RETRY_MAX, interval=RETRY_INTERVALS_SECONDS),
        failure_ttl=int(FAILURE_TTL.total_seconds()),
    )


def enqueue_cleanup(queue: Queue) -> Job:
    """Put a `cleanup()` job on the queue (called hourly by the scheduler).

    No retry: the job is idempotent and the next hour's run does the same
    work anyway.

    `ttl` is how long the job may WAIT in the queue before RQ throws it
    away unrun (RQ drops a job whose Redis key has expired). With the
    worker stopped for a day, this keeps the queue from collecting 24
    cleanup jobs that would then all run back to back; at most the newest
    one is still there when the worker returns.
    """
    return queue.enqueue(
        CLEANUP,
        ttl=int(CLEANUP_INTERVAL.total_seconds()),
        failure_ttl=int(FAILURE_TTL.total_seconds()),
    )
