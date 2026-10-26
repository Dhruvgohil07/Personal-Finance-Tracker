"""The scheduler process: `python -m app.workers.scheduler` (runs in Docker).

A clock, and nothing else. Every hour it ENQUEUES the cleanup job; the RQ
worker is what runs it (SPEC §11: "the scheduler as a separate small
process"). Keeping the work out of this process is the point:

- a slow or failing cleanup can never delay the clock, because the clock
  only writes one small job into Redis and goes back to sleep;
- the work gets everything the worker gives every job: its own process,
  logging with redaction, the database setup, the failed-job registry.

It uses APScheduler's `BlockingScheduler`, the variant that takes over
the process's main thread (`start()` does not return). That is all this
process does, so nothing needs a background thread.

**Run exactly one.** Two schedulers would enqueue cleanup twice an hour.
That is harmless (the job is idempotent) but pointless; docker-compose
starts one.
"""

import signal
from datetime import UTC, datetime
from types import FrameType

import structlog
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from redis.exceptions import RedisError
from rq import Queue

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.redis import get_redis
from app.workers.queue import CLEANUP_INTERVAL, QUEUE_NAME, enqueue_cleanup

log = structlog.get_logger()

CLEANUP_SCHEDULE_ID = "cleanup"


def enqueue_cleanup_now(queue: Queue) -> None:
    """What the scheduler calls every hour: enqueue one cleanup job.

    A Redis outage is logged and otherwise ignored. Raising would only make
    APScheduler log a traceback; the next hour tries again either way.
    """
    try:
        job = enqueue_cleanup(queue)
    except RedisError:
        log.warning("cleanup_enqueue_failed")
        return
    log.info("cleanup_enqueued", job_id=job.id)


def build_scheduler(queue: Queue) -> BlockingScheduler:
    """A scheduler with the cleanup job registered, not started yet.

    Separate from `main()` so a test can check what is registered without
    starting a clock.

    - `next_run_time=now`: run once at startup too, instead of waiting an
      hour, so a restart (after a deploy, after downtime) cleans up at once.
    - `coalesce=True`: if several runs were missed (the machine slept), run
      once, not once per missed hour.
    - `max_instances=1`: never two at the same time in this process.
    """
    scheduler = BlockingScheduler(timezone=UTC)
    scheduler.add_job(
        enqueue_cleanup_now,
        trigger=IntervalTrigger(seconds=int(CLEANUP_INTERVAL.total_seconds()), timezone=UTC),
        args=[queue],
        id=CLEANUP_SCHEDULE_ID,
        next_run_time=datetime.now(UTC),
        coalesce=True,
        max_instances=1,
    )
    return scheduler


def main() -> None:
    """Configure logging, then enqueue the cleanup every hour until stopped."""
    settings = get_settings()
    configure_logging(settings.env)
    # The app's shared client: its 2 s timeouts suit a process that sends
    # one short command an hour (unlike the worker, which blocks waiting).
    queue = Queue(QUEUE_NAME, connection=get_redis())
    scheduler = build_scheduler(queue)

    def stop(signum: int, frame: FrameType | None) -> None:
        """SIGTERM (`docker compose stop`): stop the clock and exit cleanly.

        Needed because this process is PID 1 in its container, and Linux
        IGNORES a signal sent to PID 1 unless the process installed a
        handler for it. Without this, every stop would hang for Docker's
        10-second grace period and then end in a SIGKILL. (The worker needs
        nothing like this: RQ installs its own handlers.)
        """
        log.info("scheduler_stopping")
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, stop)
    log.info("scheduler_started", every_seconds=int(CLEANUP_INTERVAL.total_seconds()))
    scheduler.start()


if __name__ == "__main__":
    main()
