"""Rate limits against the real app, Postgres and Redis (SPEC §7.1).

The counters live in Redis; the autouse `_reset_rate_limits` fixture in
conftest.py empties them before each test. All TestClient requests come
from the same client address, so "per IP" means "per test" here.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from redis.exceptions import ConnectionError as RedisConnectionError
from structlog.testing import capture_logs

from app.api.deps import CurrentUser
from app.core.config import get_settings
from app.core.rate_limit import limiter

pytestmark = pytest.mark.integration

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
HEALTH = "/api/v1/health"
WHOAMI = "/test/whoami"

PASSWORD = "correct horse battery"
AUTH_LIMIT_COUNT = 5  # AUTH_LIMIT = "5/minute"
GENERAL_LIMIT_COUNT = 100  # GENERAL_LIMIT = "100/minute"


def _register(client: TestClient, email: str) -> None:
    response = client.post(REGISTER, json={"email": email, "password": PASSWORD, "name": "Dhruv"})
    assert response.status_code == 201


def _login(client: TestClient, email: str, password: str = PASSWORD) -> Response:
    return client.post(LOGIN, json={"email": email, "password": password})


def _auth_headers(client: TestClient, email: str) -> dict[str, str]:
    """Register + log in; return the Authorization header of that user."""
    _register(client, email)
    access_token = _login(client, email).json()["access_token"]
    return {"Authorization": f"Bearer {access_token}"}


def _assert_rate_limited(response: Response) -> None:
    assert response.status_code == 429
    assert response.json() == {
        "error": {
            "code": "RATE_LIMITED",
            "message": "Too many requests. Please try again later.",
            "details": None,
        }
    }
    # Tells the client how many seconds to wait; the window is one minute.
    assert 1 <= int(response.headers["Retry-After"]) <= 60


@pytest.fixture
def protected_client(client: TestClient) -> TestClient:
    """The client plus a tiny route behind `CurrentUser` (no real one exists yet)."""

    def whoami(user: CurrentUser) -> dict[str, str]:
        return {"user_id": str(user.id)}

    app = client.app
    assert isinstance(app, FastAPI)
    app.add_api_route(WHOAMI, whoami)
    return client


# --- Login / register: 5 per minute per IP ----------------------------------


def test_sixth_login_in_a_minute_is_429_even_with_the_right_password(
    client: TestClient,
) -> None:
    _register(client, "dhruv@example.com")

    # Failed attempts count too: that is the point (password guessing).
    for _ in range(AUTH_LIMIT_COUNT):
        assert _login(client, "dhruv@example.com", "wrong password").status_code == 401
    _assert_rate_limited(_login(client, "dhruv@example.com", "wrong password"))
    _assert_rate_limited(_login(client, "dhruv@example.com"))


def test_429_passes_through_the_other_middleware(client: TestClient) -> None:
    # The limiter is the innermost middleware, so its 429 still gets the
    # CORS header (the frontend can read it), security headers and a
    # request ID, like any other response.
    origin = get_settings().frontend_origin
    for _ in range(AUTH_LIMIT_COUNT):
        _login(client, "nobody@example.com")

    response = client.post(
        LOGIN,
        json={"email": "nobody@example.com", "password": PASSWORD},
        headers={"Origin": origin},
    )

    assert response.status_code == 429
    assert response.headers["Access-Control-Allow-Origin"] == origin
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "X-Request-ID" in response.headers


def test_rate_limit_log_contains_no_ip_or_email(client: TestClient) -> None:
    for _ in range(AUTH_LIMIT_COUNT):
        _login(client, "nobody@example.com")

    with capture_logs() as logs:
        _login(client, "nobody@example.com")

    events = [entry for entry in logs if entry["event"] == "rate_limited"]
    assert events == [
        {"event": "rate_limited", "log_level": "info", "path": LOGIN, "limit": "5 per 1 minute"}
    ]
    # TestClient's client address is "testclient"; it must not appear anywhere.
    assert "testclient" not in str(logs)
    assert "nobody@example.com" not in str(logs)


def test_register_and_login_have_separate_counters(client: TestClient) -> None:
    """Using up the login limit must not block registering.

    The Redis key of a decorated route is the client IP plus the route's
    path, so /auth/login and /auth/register count separately. Otherwise a
    user who mistyped their password 5 times couldn't create an account
    from the same network for a minute.
    """
    for _ in range(AUTH_LIMIT_COUNT):
        assert _login(client, "unknown@example.com", "wrong password").status_code == 401
    _assert_rate_limited(_login(client, "unknown@example.com", "wrong password"))
    assert (
        client.post(
            REGISTER,
            json={"email": "newuser@example.com", "password": PASSWORD, "name": "New User"},
        ).status_code
        == 201
    )


# --- General API limit: 100 per minute per user ------------------------------


def test_general_limit_is_per_user(protected_client: TestClient) -> None:
    client = protected_client
    first = _auth_headers(client, "first@example.com")
    second = _auth_headers(client, "second@example.com")

    for _ in range(GENERAL_LIMIT_COUNT):
        assert client.get(WHOAMI, headers=first).status_code == 200
    _assert_rate_limited(client.get(WHOAMI, headers=first))

    # Same IP, different user: the second user is unaffected.
    assert client.get(WHOAMI, headers=second).status_code == 200


def test_health_is_never_rate_limited(client: TestClient) -> None:
    for _ in range(GENERAL_LIMIT_COUNT + 1):
        assert client.get(HEALTH).status_code != 429


# --- Redis down: fail open -----------------------------------------------------


def test_requests_still_work_when_redis_is_down(
    protected_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Make every counter update fail as if Redis were unreachable.
    # (Regression test for the slowapi bug worked around in RateLimitMiddleware:
    # without it, these requests would be 500s.)
    def redis_down(*args: object, **kwargs: object) -> bool:
        raise RedisConnectionError("connection refused")

    monkeypatch.setattr(limiter.limiter, "hit", redis_down)
    client = protected_client

    # Decorated routes (register, login) and the general limit (whoami)
    # both let the request through instead of failing.
    headers = _auth_headers(client, "dhruv@example.com")
    assert client.get(WHOAMI, headers=headers).status_code == 200
