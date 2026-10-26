"""The scheduler process, without a clock, Redis or Postgres.

"Registers the job" is checked by building the scheduler and reading what
it holds, and the registered function is then called by hand: no test
waits for an hour, or for anything.
"""

from typing import Any

import pytest
from apscheduler.triggers.interval import IntervalTrigger
from redis.exceptions import ConnectionError as RedisConnectionError
from rq.utils import import_attribute

from app.workers import jobs, scheduler
from app.workers.queue import CLEANUP, CLEANUP_INTERVAL, FAILURE_TTL, QUEUE_NAME, enqueue_cleanup


class FakeQueue:
    """Records `enqueue()` calls; optionally fails like Redis being down."""

    def __init__(self, *, fail: bool = False, name: str = QUEUE_NAME) -> None:
        self.name = name
        self.fail = fail
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def enqueue(self, *args: Any, **kwargs: Any) -> Any:
        if self.fail:
            raise RedisConnectionError("Redis is down")
        self.calls.append((args, kwargs))
        return type("FakeJob", (), {"id": "job-1"})()


def test_the_cleanup_is_registered_every_hour_and_once_at_start() -> None:
    queue = FakeQueue()

    built = scheduler.build_scheduler(queue)  # type: ignore[arg-type]

    [job] = built.get_jobs()
    assert job.id == scheduler.CLEANUP_SCHEDULE_ID
    assert job.func is scheduler.enqueue_cleanup_now
    assert job.args == (queue,)
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval == CLEANUP_INTERVAL
    # Missed runs collapse into one, and runs never overlap.
    assert job.coalesce is True
    assert job.max_instances == 1


def test_the_scheduled_function_only_enqueues_the_cleanup_job() -> None:
    """The scheduler does no work itself: it puts one job on the queue."""
    queue = FakeQueue()

    scheduler.enqueue_cleanup_now(queue)  # type: ignore[arg-type]

    [(args, kwargs)] = queue.calls
    assert args == (CLEANUP,)
    # Waiting longer than an hour in the queue makes it pointless: the next
    # one is already on its way.
    assert kwargs["ttl"] == CLEANUP_INTERVAL.total_seconds()
    assert kwargs["failure_ttl"] == FAILURE_TTL.total_seconds()


def test_redis_being_down_is_logged_not_raised() -> None:
    """The next hour tries again; the scheduler process must keep running."""
    scheduler.enqueue_cleanup_now(FakeQueue(fail=True))  # type: ignore[arg-type]


def test_main_starts_the_scheduler_on_the_workers_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[Any] = []

    class FakeScheduler:
        def __init__(self, queue: Any) -> None:
            self.queue = queue

        def start(self) -> None:
            started.append(self.queue)

    monkeypatch.setattr(scheduler, "build_scheduler", FakeScheduler)

    scheduler.main()

    [queue] = started
    assert queue.name == QUEUE_NAME


def test_the_cleanup_job_name_resolves_to_the_job() -> None:
    assert import_attribute(CLEANUP) is jobs.cleanup


def test_enqueue_cleanup_returns_the_job() -> None:
    assert enqueue_cleanup(FakeQueue()).id == "job-1"  # type: ignore[arg-type]
