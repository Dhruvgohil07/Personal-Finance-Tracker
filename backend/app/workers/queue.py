"""The RQ queue: where the API leaves work for the worker (SPEC §11).

How RQ works, in one paragraph: `queue.enqueue(func, *args)` writes a
*job* into Redis - the function's dotted name, its arguments (pickled) and
some options - and pushes the job's id onto a Redis list. A separate
worker process (app/workers/worker.py) waits on that list, takes the id,
loads the job and calls the function. The API and the worker never talk to
each other directly; Redis sits between them.

Two rules from the SPEC shape this module:

- **A job's only argument is the upload id** (SPEC §11, CLAUDE.md §7.2).
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
from datetime import timedelta

from rq import Callback, Queue, Retry
from rq.job import Job

from app.core.redis import get_redis

# One queue for now. The worker listens on the same name.
QUEUE_NAME = "default"

PROCESS_STATEMENT = "app.workers.jobs.process_statement"
ON_PROCESS_STATEMENT_FAILURE = "app.workers.jobs.on_process_statement_failure"

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
        retry=Retry(max=RETRY_MAX, interval=RETRY_INTERVALS_SECONDS),
        on_failure=Callback(ON_PROCESS_STATEMENT_FAILURE),
        failure_ttl=int(FAILURE_TTL.total_seconds()),
    )
