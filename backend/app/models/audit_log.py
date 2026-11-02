"""Audit log (SPEC §5 `audit_log`): a record of security-relevant actions.

One row per action, written in the same database transaction as the action
itself, so the record exists exactly when the action happened. Rows are
never updated, which is why there is no `updated_at`.

The first action recorded is deleting a user (`DELETE /me`, ADR 021).
That row must outlive the user it describes, so `user_id` deliberately has
**no foreign key** to `users`: with one, the database would either refuse
the delete or (with `ON DELETE CASCADE`) delete the audit row along with
the user, which defeats the point.

What is never stored here (SPEC §5, CLAUDE.md §7): emails, names, amounts,
balances, narrations. Ids, the action, and where the request came from only.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin, str_enum_column

# Longest User-Agent kept. The header is chosen by the client, so without a
# cap anyone could make us store kilobytes per request. Real browsers send
# about 100-250 characters.
MAX_USER_AGENT_LENGTH = 512


class AuditAction(StrEnum):
    # The user deleted their own Kharcha profile and all their data.
    # Not "account_deleted" (SPEC §5's example): in this codebase an
    # "account" is a bank account (app/models/account.py).
    USER_DELETED = "user_deleted"


class AuditLog(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_log"

    # Who did it. No foreign key, on purpose (see the module docstring).
    # Nullable for actions with no known user yet, e.g. a failed login for
    # an email that has no account (a later phase may record those).
    user_id: Mapped[uuid.UUID | None]
    # VARCHAR + CHECK like every enum column (ADR 002). A new action is a
    # new enum member plus a migration that widens the CHECK.
    action: Mapped[AuditAction] = mapped_column(str_enum_column(AuditAction, "action"))
    # What the action was done to: a table-like name ("user") and its id.
    entity: Mapped[str]
    entity_id: Mapped[uuid.UUID | None]
    # Where the request came from. Text, not Postgres INET: the value is
    # whatever the server saw as the client address, and test clients use
    # names like "testclient" that INET would reject.
    ip: Mapped[str | None]
    user_agent: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        # No ip or user agent: they describe a person's device (rule 3).
        return f"AuditLog(id={self.id}, action={self.action.value!r})"
