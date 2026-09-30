"""Tests for the rate-limit key functions (app/core/rate_limit.py).

The key decides WHO a request is counted against. These are pure functions
of the request, so no Redis is needed; the limits themselves are tested in
tests/integration/test_rate_limit.py.
"""

import uuid
from datetime import UTC, datetime, timedelta

from starlette.requests import Request

from app.core.rate_limit import client_ip, user_or_ip_key
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
