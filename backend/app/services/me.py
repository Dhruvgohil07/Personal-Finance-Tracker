"""The logged-in user's own profile: deleting it (ADR 021).

GET /me needs no service: the route already has the user from
`get_current_user` and only converts it to a response model.
"""

import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.errors import IncorrectPasswordError
from app.core.security import verify_password
from app.models import AuditAction, AuditLog, User
from app.models.audit_log import MAX_USER_AGENT_LENGTH

log = structlog.get_logger()

# Named without "password", so the security linter (S105) does not take the
# message for a hardcoded password.
DELETE_REFUSED_MESSAGE = "That password is not correct."


@dataclass(frozen=True)
class RequestOrigin:
    """Where a request came from, as the audit log records it.

    The route reads these from the HTTP request; the service only stores
    them, so it never has to know about FastAPI's `Request`.
    """

    ip: str | None
    user_agent: str | None


def delete_user(db: Session, user: User, *, password: str, origin: RequestOrigin) -> None:
    """Hard-delete the user and ALL of their data, then record that it happened.

    1. **The password is checked again.** An access token alone (maybe
       stolen from an unlocked laptop) must not be enough. A wrong password
       changes nothing and raises IncorrectPasswordError (400).
    2. **One DELETE of the users row.** Every table holding a user's data
       has a foreign key to `users` with `ON DELETE CASCADE` (SPEC §5), so
       Postgres removes their accounts, uploads, upload blobs, transactions,
       rules, own categories and refresh tokens in the same statement.
       Deleting the refresh tokens is what ends every session: the old
       cookie no longer matches any row, so /auth/refresh answers 401, and
       `get_current_user` answers 401 for an access token whose user is gone.
    3. **An audit row is added in the same transaction.** Either both the
       delete and its record are committed, or neither is.

    A running import holds a lock on the user row (app/services/imports.py,
    "Lock order"), so the DELETE waits until it has committed, then removes
    its rows too. A job still waiting in the queue later finds its upload
    gone and does nothing.
    """
    if not verify_password(password, user.password_hash):
        log.info("user_delete_refused", user_id=str(user.id))
        raise IncorrectPasswordError(DELETE_REFUSED_MESSAGE)

    # Kept in a local variable: after the DELETE, `user` is an object for a
    # row that no longer exists, and its attributes shouldn't be relied on.
    user_id: uuid.UUID = user.id

    # A bulk `delete()` statement rather than `db.delete(user)`: the database
    # does the cascading, and the ORM doesn't first load every related row.
    db.execute(delete(User).where(User.id == user_id))
    db.add(
        AuditLog(
            user_id=user_id,
            action=AuditAction.USER_DELETED,
            entity="user",
            entity_id=user_id,
            ip=origin.ip,
            user_agent=_truncate(origin.user_agent, MAX_USER_AGENT_LENGTH),
        )
    )
    db.commit()

    # The id only: no email, which was the last place it existed.
    log.info("user_deleted", user_id=str(user_id))


def _truncate(value: str | None, max_length: int) -> str | None:
    return value[:max_length] if value is not None else None
