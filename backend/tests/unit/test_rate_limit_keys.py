"""Tests for the Redis-free parts of app/core/rate_limit.py.

Two groups:

- the KEY functions, which decide who a request is counted against;
- `_retry_after_seconds`, which reads how long a client must wait.

Both are functions of the request, so no Redis is needed here; the limits
themselves are tested against real Redis in
tests/integration/test_rate_limit.py.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from limits import RateLimitItemPerMinute
from redis.exceptions import RedisError
from starlette.requests import Request

from app.core import rate_limit
from app.core.rate_limit import (
    _retry_after_seconds,
    client_ip,
    limiter,
    user_or_ip_key,
)
from app.core.security import create_access_token

USER_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def make_request(*, authorization: str | None = None, forwarded_for: str | None = None) -> Request:
    """A bare Starlette Request from client 203.0.113.7 (a documentation IP).

    A "scope" is the dict an ASGI server builds for each request; headers
    are (name, value) byte pairs with lowercase names.
    """
    headers = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode()))
    scope = {"type": "http", "headers": headers, "client": ("203.0.113.7", 50000)}
    return Request(scope)


def test_client_ip_uses_the_connection_address() -> None:
    assert client_ip(make_request()) == "ip:203.0.113.7"


def test_client_ip_ignores_x_forwarded_for() -> None:
    # Otherwise an attacker could pick a new fake IP for every request.
    request = make_request(forwarded_for="198.51.100.1")
    assert client_ip(request) == "ip:203.0.113.7"


def test_client_ip_without_client_info() -> None:
    request = Request({"type": "http", "headers": [], "client": None})
    assert client_ip(request) == "ip:unknown"


def test_valid_access_token_is_counted_per_user() -> None:
    token = create_access_token(USER_ID)
    request = make_request(authorization=f"Bearer {token}")
    assert user_or_ip_key(request) == f"user:{USER_ID}"


def test_bearer_scheme_is_case_insensitive() -> None:
    token = create_access_token(USER_ID)
    request = make_request(authorization=f"bearer {token}")
    assert user_or_ip_key(request) == f"user:{USER_ID}"


def test_no_token_is_counted_per_ip() -> None:
    assert user_or_ip_key(make_request()) == "ip:203.0.113.7"


def test_garbage_token_is_counted_per_ip() -> None:
    # Sending an invalid token must not create a fresh counter.
    request = make_request(authorization="Bearer not-a-jwt")
    assert user_or_ip_key(request) == "ip:203.0.113.7"


def test_expired_token_is_counted_per_ip() -> None:
    long_ago = datetime.now(UTC) - timedelta(days=1)
    token = create_access_token(USER_ID, now=long_ago)
    request = make_request(authorization=f"Bearer {token}")
    assert user_or_ip_key(request) == "ip:203.0.113.7"


def test_other_auth_scheme_is_counted_per_ip() -> None:
    request = make_request(authorization="Basic dXNlcjpwYXNz")
    assert user_or_ip_key(request) == "ip:203.0.113.7"


# --- Retry-After ----------------------------------------------------------
#
# When a limit is hit, slowapi leaves the limit it hit on the request as
# `state.view_rate_limit = (limit_item, key_parts)`, and
# `_retry_after_seconds` asks the storage when that window ends. The header
# is advisory, so every failure answers None ("no Retry-After header")
# instead of breaking the 429 the client is waiting for.

LIMIT_ITEM = RateLimitItemPerMinute(5)
KEY_PARTS = ("ip:203.0.113.7",)


def _request_that_hit_a_limit() -> Request:
    request = make_request()
    request.state.view_rate_limit = (LIMIT_ITEM, KEY_PARTS)
    return request


def test_retry_after_is_unknown_without_a_recorded_limit() -> None:
    # The 429 handler also runs for a RateLimitExceeded raised anywhere
    # else, where nothing recorded WHICH window was full.
    assert _retry_after_seconds(make_request()) is None


def test_retry_after_is_unknown_when_redis_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fail open on the header too: a Redis outage must not turn a 429 into
    # a 500 (ADR 007).
    def boom(*args: object, **kwargs: object) -> None:
        raise RedisError("connection refused")

    monkeypatch.setattr(limiter.limiter, "get_window_stats", boom)
    assert _retry_after_seconds(_request_that_hit_a_limit()) is None


@pytest.mark.parametrize(
    ("seconds_left", "expected"),
    [
        (0.2, 1),  # never "Retry-After: 0", which reads as "try again now"
        (4.2, 5),  # always rounded UP, so the retry can't be too early
        (60.0, 60),
    ],
)
def test_retry_after_rounds_up_to_whole_seconds(
    monkeypatch: pytest.MonkeyPatch, seconds_left: float, expected: int
) -> None:
    # `rate_limit.time` IS the standard time module, so this freezes the
    # clock the function reads (monkeypatch puts it back afterwards).
    # Without it the few microseconds between the two lines below would
    # decide the 4.2 -> 5 case, which is exactly the rounding being tested.
    now = 1_000_000.0
    monkeypatch.setattr(rate_limit.time, "time", lambda: now)
    monkeypatch.setattr(limiter.limiter, "get_window_stats", lambda *a: (now + seconds_left, 0))

    assert _retry_after_seconds(_request_that_hit_a_limit()) == expected
