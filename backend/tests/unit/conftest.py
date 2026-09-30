"""Fixtures shared by all unit tests."""

from collections.abc import Iterator

import pytest

from app.core.rate_limit import limiter


@pytest.fixture(autouse=True)
def _rate_limiting_off() -> Iterator[None]:
    """Unit tests must not need Redis, where the rate-limit counters live.

    Several unit tests build the whole app (create_app) and send requests
    to it. With the limiter on, each request would talk to Redis. The rate
    limits themselves are tested against real Redis in
    tests/integration/test_rate_limit.py.

    `autouse=True`: applied to every test in this folder automatically.
    """
    limiter.enabled = False
    yield
    limiter.enabled = True
