"""Tests for app.db.session: the real DB dependencies and the health engine.

Every API test replaces get_db / get_health_db with a fake, so the real
ones are tested here directly.
"""

from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.db.session import (
    HEALTH_CONNECT_TIMEOUT_SECONDS,
    build_health_engine,
    get_db,
    get_health_db,
    health_engine,
)


@pytest.mark.parametrize("dependency_function", [get_db, get_health_db])
def test_db_dependency_yields_a_session_and_always_closes_it(
    dependency_function: Callable[[], Iterator[Session]],
) -> None:
    # Creating a Session does not connect to Postgres yet, so no DB is needed.
    dependency = dependency_function()
    session = next(dependency)  # runs the function up to its `yield`
    assert isinstance(session, Session)

    # FastAPI "finishes" the generator after the response is sent. We do the
    # same with .close(), which runs the function's `finally` block.
    with patch.object(session, "close") as close:
        dependency.close()

    close.assert_called_once()


def test_get_health_db_uses_the_health_engine() -> None:
    dependency = get_health_db()
    session = next(dependency)

    assert session.get_bind() is health_engine
    dependency.close()


class _StopBeforeConnecting(Exception):
    """Raised by our listener so no real connection is attempted."""


def test_health_engine_fails_fast_and_has_no_pool() -> None:
    # Port 1: nothing listens there, but we never actually connect anyway.
    engine = build_health_engine("postgresql+psycopg://user:pass@127.0.0.1:1/db")
    seen: dict[str, Any] = {}

    # "do_connect" is a SQLAlchemy event that fires right before the driver
    # (psycopg) opens a connection, with the exact arguments it will get.
    # We record them and stop there.
    @event.listens_for(engine, "do_connect")
    def capture(dialect: Any, conn_rec: Any, cargs: Any, cparams: dict[str, Any]) -> None:
        seen.update(cparams)
        raise _StopBeforeConnecting

    with pytest.raises(_StopBeforeConnecting):
        engine.connect()

    assert seen["connect_timeout"] == HEALTH_CONNECT_TIMEOUT_SECONDS == 3
    assert isinstance(engine.pool, NullPool)
