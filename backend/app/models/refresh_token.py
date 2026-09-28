"""Refresh tokens (SPEC §5 `refresh_tokens`).

One row per refresh token ever issued. Rows are revoked (not deleted) when
used, so we can recognise a token that is presented a second time.

Token families: logging in starts a new family (`family_id`). Each refresh
revokes the old token and issues a new one in the SAME family. If a revoked
token is ever presented again, someone is replaying a stolen token (or the
real user is, after the thief used it first), so the whole family is revoked
and both parties must log in again. The logic lives in the auth service
(Step 2); this table only stores the state.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin


class RefreshToken(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "refresh_tokens"

    # index=True: Postgres does NOT index foreign-key columns automatically.
    # We need it for "revoke all of this user's tokens" and so that deleting
    # a user can find their tokens quickly.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # HMAC-SHA256 of the token (hex). The raw token is never stored.
    # unique=True also creates the index used to look a token up by its hash.
    token_hash: Mapped[str] = mapped_column(unique=True)
    # Indexed for "revoke the whole family" on reuse detection.
    family_id: Mapped[uuid.UUID] = mapped_column(index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # NULL = still usable. Set when rotated, on logout, or on reuse detection.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # No updated_at (unlike TimestampMixin): the only change a row ever gets
    # is revoked_at being set, which records the time itself.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"RefreshToken(id={self.id}, family_id={self.family_id})"
