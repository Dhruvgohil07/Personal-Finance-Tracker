"""Tests for the real get_db dependency (every API test replaces it with a fake)."""

from unittest.mock import patch

from sqlalchemy.orm import Session

from app.db.session import get_db


def test_get_db_yields_a_session_and_always_closes_it() -> None:
    # Creating a Session does not connect to Postgres yet, so no DB is needed.
    dependency = get_db()
    session = next(dependency)  # runs get_db up to its `yield`
    assert isinstance(session, Session)

    # FastAPI "finishes" the generator after the response is sent. We do the
    # same with .close(), which runs get_db's `finally` block.
    with patch.object(session, "close") as close:
        dependency.close()

    close.assert_called_once()
