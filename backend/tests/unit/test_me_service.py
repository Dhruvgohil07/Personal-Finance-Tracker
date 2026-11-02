"""The deadlock retry in `delete_user`, without a database (ADR 021).

tests/integration/test_me.py makes Postgres abort a delete for real once.
The two branches below would need Postgres to choose the delete as the
victim several times in a row, or to fail for another reason, so they are
tested here with the one database step replaced: `_delete_and_audit`
raises the error SQLAlchemy would raise.
"""

import uuid

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.errors import DEADLOCK_SQLSTATE
from app.models import User
from app.services import me as me_service
from app.services.me import MAX_DELETE_ATTEMPTS, RequestOrigin

PASSWORD = "correct horse battery"
ORIGIN = RequestOrigin(ip="203.0.113.7", user_agent="test")
# SQLSTATE 57014, "query_canceled": a database error that is not a deadlock.
QUERY_CANCELED_SQLSTATE = "57014"


class FakePsycopgError(Exception):
    """Stands in for the psycopg error SQLAlchemy wraps; only `sqlstate` is read."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class FakeSession:
    """Only what delete_user calls on the session when the step fails."""

    def __init__(self) -> None:
        self.rollbacks = 0

    def rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture(scope="module")
def user() -> User:
    return User(
        id=uuid.uuid4(),
        email="alice@example.com",
        password_hash=hash_password(PASSWORD),
        name="Alice",
    )


def failing_step(monkeypatch: pytest.MonkeyPatch, sqlstate: str) -> list[uuid.UUID]:
    """Make every delete attempt fail with this SQLSTATE; return the attempt log."""
    attempts: list[uuid.UUID] = []

    def fail(db: Session, user_id: uuid.UUID, origin: RequestOrigin) -> bool:
        attempts.append(user_id)
        raise OperationalError("DELETE FROM users ...", {}, FakePsycopgError(sqlstate))

    monkeypatch.setattr(me_service, "_delete_and_audit", fail)
    return attempts


def test_a_deadlock_on_every_attempt_is_raised_after_the_last(
    monkeypatch: pytest.MonkeyPatch, user: User
) -> None:
    attempts = failing_step(monkeypatch, DEADLOCK_SQLSTATE)
    db = FakeSession()

    with pytest.raises(OperationalError):
        me_service.delete_user(db, user, password=PASSWORD, origin=ORIGIN)

    assert attempts == [user.id] * MAX_DELETE_ATTEMPTS
    # Every aborted transaction is ended before the next try.
    assert db.rollbacks == MAX_DELETE_ATTEMPTS


def test_another_database_error_is_not_retried(monkeypatch: pytest.MonkeyPatch, user: User) -> None:
    attempts = failing_step(monkeypatch, QUERY_CANCELED_SQLSTATE)
    db = FakeSession()

    with pytest.raises(OperationalError):
        me_service.delete_user(db, user, password=PASSWORD, origin=ORIGIN)

    assert attempts == [user.id]
    assert db.rollbacks == 1
