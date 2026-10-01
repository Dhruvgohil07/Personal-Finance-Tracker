"""Fixtures for integration tests against the real `kharcha_test` Postgres DB.

Needs `docker compose up -d` and TEST_DATABASE_URL in `.env`.

Lifecycle:
- once per test run  (`migrated_engine`): wipe the test DB and build the
  schema with `alembic upgrade head`, exactly like production does, so the
  migrations themselves are tested too.
- once per test      (`db`): hand out a Session, then empty every table
  afterwards so each test starts from a clean database.
- once per test      (`client`): a TestClient for the real app whose routes
  use the test database.
- before every test  (`_reset_rate_limits`, automatic): clear the rate-limit
  counters in Redis.
"""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.db.base import Base
from app.db.session import build_engine, get_db
from app.main import create_app

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _test_database_url() -> str:
    url = get_settings().test_database_url
    if url is None:
        pytest.fail("TEST_DATABASE_URL is not set; integration tests need it (see .env.example)")
    return url


@pytest.fixture(autouse=True)
def _reset_rate_limits() -> None:
    """Start every test with empty rate-limit counters.

    All TestClient requests come from the same fake client address
    ("testclient"), so without this, the logins of earlier tests would use
    up the 5/minute login limit of later ones.

    reset() deletes every key the `limits` library created in Redis (they
    all start with "LIMITS:"). Tests and local development share one Redis,
    so this also clears development counters, which is harmless.
    """
    limiter.reset()


@pytest.fixture(scope="session")
def alembic_config() -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    # alembic.ini uses "%" for its own variables, so a literal "%" in the URL
    # (e.g. an URL-encoded password) must be doubled.
    config.set_main_option("sqlalchemy.url", _test_database_url().replace("%", "%%"))
    return config


@pytest.fixture(scope="session")
def migrated_engine(alembic_config: Config) -> Iterator[Engine]:
    engine = build_engine(_test_database_url())

    # Start from an empty database: drop and recreate the whole schema
    # (tables, extensions, the alembic_version table: everything).
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))

    command.upgrade(alembic_config, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db(migrated_engine: Engine) -> Iterator[Session]:
    session = sessionmaker(bind=migrated_engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.rollback()  # discard anything the test left uncommitted
        session.close()
        # Empty every table the models know about. The table names come from
        # our own metadata (not user input), so building this string is safe.
        table_names = ", ".join(t.name for t in Base.metadata.sorted_tables)
        with migrated_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {table_names} CASCADE"))


@pytest.fixture
def client(migrated_engine: Engine, db: Session) -> Iterator[TestClient]:
    """The real app, with every request getting its own test-DB session.

    Depending on `db` means the tables are emptied after the test.

    A fresh session per request (not the shared `db` one) behaves like
    production: nothing cached in memory carries over between requests.

    base_url is https because the refresh cookie is `Secure`: the test
    client, like a browser, would not send it back over plain http.
    """
    session_factory = sessionmaker(bind=migrated_engine, expire_on_commit=False)

    def _get_test_db() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app = create_app()
    app.dependency_overrides[get_db] = _get_test_db
    # Not `with TestClient(...)`: that would run the app's shutdown code,
    # which closes the shared engine and Redis client used by other tests.
    yield TestClient(app, base_url="https://testserver")


@pytest.fixture
def make_auth_headers(client: TestClient) -> Callable[[str], dict[str, str]]:
    """Register + log in a user through the real API; return their auth header.

    A factory (a fixture that returns a function), because IDOR tests need
    TWO users: one who owns the data and one who tries to reach it.
    Each call uses one register and one login, well within the 5/minute
    limits (the counters are reset before every test).
    """

    def _make(email: str) -> dict[str, str]:
        password = "correct horse battery"
        response = client.post(
            "/api/v1/auth/register", json={"email": email, "password": password, "name": "Test"}
        )
        assert response.status_code == 201
        response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert response.status_code == 200
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    return _make
