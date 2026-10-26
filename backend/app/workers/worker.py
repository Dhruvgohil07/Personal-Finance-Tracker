"""The worker process: `python -m app.workers.worker` (runs in Docker).

The SPEC suggests starting the worker with RQ's own `rq worker` command.
This small module is used instead for one reason: **logging**. The jobs log
through structlog, and only `configure_logging` installs the processor that
redacts sensitive fields (CLAUDE.md rule 3). `rq worker` knows nothing
about our app, so with it the jobs would log with structlog's default
settings and no redaction. Here it is configured first, and then exactly
what `rq worker` does is done: build a `Worker` and call `work()`.

Why Docker: RQ's worker runs each job in a child process created with
`fork()`, which Windows does not have (Phase 2 plan, constraint 6). The
`worker` service in docker-compose.yml runs this module on Linux.

The jobs open their pooled database connections in the forked child. The
parent touches Postgres only in the two failure hooks that RQ runs there
(a dead child, an abandoned job), and those use an unpooled engine that
keeps no connection open to be inherited by the next child (a psycopg
connection must not be used by two processes).
"""

from redis import Redis
from rq import Worker

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.workers import jobs
from app.workers.queue import QUEUE_NAME


def main() -> None:
    """Configure logging, then process jobs until the process is stopped."""
    settings = get_settings()
    configure_logging(settings.env)

    # Its own Redis client, not the API's `get_redis()`: the worker waits
    # for new jobs with a blocking Redis command, and RQ raises the client's
    # socket timeout to fit that wait. Doing that on the shared client would
    # change it for everything else in the process.
    connection = Redis.from_url(settings.redis_url)
    # work_horse_killed_handler: when a job's child process DIES (out of
    # memory, a crash), RQ calls this instead of the job's on_failure
    # callback, so without it such an upload would stay `processing`.
    worker = Worker(
        [QUEUE_NAME],
        connection=connection,
        work_horse_killed_handler=jobs.on_work_horse_killed,
    )
    # with_scheduler=True: a retry with a delay (Retry(interval=...)) is
    # parked in RQ's "scheduled" registry, and only a worker running the
    # scheduler moves it back onto the queue when the delay is over.
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
