"""App users (SPEC §5 `users`).

Every table holding personal data points back here with
`ON DELETE CASCADE`, so deleting a user row deletes all of their data.
"""

from typing import Any

from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# Per-user preferences. A new user gets a copy of these.
DEFAULT_USER_SETTINGS: dict[str, Any] = {
    "ai_enabled": True,  # may merchant names be sent to the LLM? (SPEC §7.4)
    "weekly_email": False,  # weekly summary email (Phase 5)
}


def _default_settings() -> dict[str, Any]:
    # A function, so every user gets their OWN dict. Using the dict itself as
    # the default would share one object between users (the classic
    # "mutable default" bug).
    return dict(DEFAULT_USER_SETTINGS)


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    # CITEXT = case-insensitive text (Postgres `citext` extension, enabled in
    # migration 0001). "Dhruv@Mail.com" and "dhruv@mail.com" count as the
    # same email for the unique constraint AND for `WHERE email = ...`, so
    # the database itself prevents two accounts with the same email.
    email: Mapped[str] = mapped_column(CITEXT, unique=True)
    # The argon2id hash (see app/core/security.py), never the password.
    password_hash: Mapped[str]
    name: Mapped[str]
    # JSONB: a JSON document stored in binary form. Used for small,
    # flexible settings that don't deserve their own columns.
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=_default_settings)

    def __repr__(self) -> str:
        # Only the id: an email in a repr could end up in logs or tracebacks.
        return f"User(id={self.id})"
