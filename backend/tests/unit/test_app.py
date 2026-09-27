"""Tests for the assembled app (app.main): health route, headers, CORS, errors.

No Postgres or Redis needed: we replace the real dependencies with fakes
through `app.dependency_overrides`. FastAPI then calls our fake instead of
get_health_db / get_redis, and the route code runs unchanged. This is one of the
big wins of dependency injection.
"""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from app import __version__
from app.core.config import get_settings
from app.core.middleware import SecurityHeadersMiddleware
from app.core.redis import get_redis
from app.db.session import get_health_db
from app.main import create_app

HEALTH_URL = "/api/v1/health"


class FakeSession:
    def __init__(self, *, fail: bool) -> None:
        self.fail = fail

    def execute(self, statement: object) -> None:
        if self.fail:
            raise OperationalError("SELECT 1", None, Exception("connection refused"))


class FakeRedis:
    def __init__(self, *, fail: bool) -> None:
        self.fail = fail

    def ping(self) -> bool:
        if self.fail:
            raise RedisConnectionError("connection refused")
        return True


def make_client(*, db_fails: bool = False, redis_fails: bool = False) -> TestClient:
    app = create_app()

    def fake_get_health_db() -> Iterator[FakeSession]:
        yield FakeSession(fail=db_fails)

    app.dependency_overrides[get_health_db] = fake_get_health_db
    app.dependency_overrides[get_redis] = lambda: FakeRedis(fail=redis_fails)
    return TestClient(app)


@pytest.fixture
def client() -> TestClient:
    return make_client()


# --- Health ---------------------------------------------------------------


def test_health_ok(client: TestClient) -> None:
    response = client.get(HEALTH_URL)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"database": "ok", "redis": "ok"},
        "version": __version__,
    }


def test_health_database_down_returns_503() -> None:
    response = make_client(db_fails=True).get(HEALTH_URL)

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"database": "unavailable", "redis": "ok"},
        # Still reported when unhealthy: that's when you most need to know it.
        "version": __version__,
    }


def test_health_redis_down_returns_503() -> None:
    response = make_client(redis_fails=True).get(HEALTH_URL)

    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "ok", "redis": "unavailable"}


def test_health_is_listed_in_openapi_docs(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    assert set(schema["paths"][HEALTH_URL]["get"]["responses"]) >= {"200", "503"}


# --- Errors ---------------------------------------------------------------


def test_unknown_api_route_uses_error_shape(client: TestClient) -> None:
    response = client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "NOT_FOUND", "message": "Not Found", "details": None}
    }


# --- Security headers -----------------------------------------------------


def test_security_headers_on_success_and_error_responses(client: TestClient) -> None:
    for response in (client.get(HEALTH_URL), client.get("/api/v1/does-not-exist")):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store"


def test_no_hsts_outside_production(client: TestClient) -> None:
    assert "strict-transport-security" not in client.get(HEALTH_URL).headers


def test_hsts_when_enabled() -> None:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware, enable_hsts=True)

    @app.get("/")
    def root() -> dict:
        return {}

    response = TestClient(app).get("/")

    assert response.headers["strict-transport-security"].startswith("max-age=")


def test_route_can_override_a_security_header() -> None:
    # setdefault in the middleware: a header set by the route wins.
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/")
    def root(response: Response) -> dict:
        response.headers["Cache-Control"] = "private, max-age=60"
        return {}

    response = TestClient(app).get("/")

    assert response.headers["cache-control"] == "private, max-age=60"


# --- CORS -----------------------------------------------------------------


def _preflight(client: TestClient, origin: str) -> dict[str, str]:
    """Send the OPTIONS request a browser sends before a cross-origin call."""
    response = client.options(
        HEALTH_URL,
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )
    return dict(response.headers)


def test_cors_allows_the_frontend_origin(client: TestClient) -> None:
    origin = get_settings().frontend_origin

    headers = _preflight(client, origin)

    assert headers["access-control-allow-origin"] == origin
    assert headers["access-control-allow-credentials"] == "true"


def test_cors_rejects_other_origins(client: TestClient) -> None:
    headers = _preflight(client, "https://evil.example.com")

    # No allow-origin header = the browser blocks the call.
    assert "access-control-allow-origin" not in headers


def test_cors_preflight_also_gets_security_headers(client: TestClient) -> None:
    # CORS answers preflights itself, so this only passes if the security
    # headers middleware wraps AROUND the CORS middleware (see main.py).
    for origin in (get_settings().frontend_origin, "https://evil.example.com"):
        assert _preflight(client, origin)["x-content-type-options"] == "nosniff"


# --- Lifespan -------------------------------------------------------------


def test_app_starts_and_shuts_down_cleanly() -> None:
    # Using TestClient as a context manager runs the lifespan: startup on
    # `with`, shutdown (engine.dispose, redis close) when the block ends.
    # Without `with`, the lifespan never runs at all.
    with make_client() as client:
        assert client.get(HEALTH_URL).status_code == 200
