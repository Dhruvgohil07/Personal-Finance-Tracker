"""Fixtures for integration tests against the real `kharcha_test` Postgres DB.

Needs `docker compose up -d` and TEST_DATABASE_URL in `.env`.

Lifecycle:
- once per test run  (`migrated_engine`): wipe the test DB and build the
  schema with `alembic upgrade head`, exactly like production does, so the
  migrations themselves are tested too.
- once per test      (`db`): hand out a Session, then empty every table
  afterwards so each test starts from a clean database.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.base import Base
from app.db.session import build_engine

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _test_database_url() -> str:
    url = get_settings().test_database_url
    if url is None:
        pytest.fail("TEST_DATABASE_URL is not set; integration tests need it (see .env.example)")
    return url


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
