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

Nothing here connects to Postgres. Each job imports `app.workers.jobs` in
its forked child process, and the database connections are opened there,
so a parent's connection is never shared with a child (a psycopg
connection must not be used by two processes).
"""

from redis import Redis
from rq import Worker

from app.core.config import get_settings
from app.core.logging import configure_logging
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
    worker = Worker([QUEUE_NAME], connection=connection)
    # with_scheduler=True: a retry with a delay (Retry(interval=...)) is
    # parked in RQ's "scheduled" registry, and only a worker running the
    # scheduler moves it back onto the queue when the delay is over.
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
