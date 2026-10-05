"""One uploaded statement file and how its import went (SPEC §5 `statement_uploads`).

Every transaction points back to the upload that created it, so this table
answers "where did this row come from?" - and, because `DELETE
/uploads/{id}` (Phase 2) removes the transactions an upload inserted, it is
also the unit of undo.

Two things about the columns are worth knowing before reading them:

1. **`unique(user_id, file_sha256)` is dedupe level 1** (SPEC §6.3). The
   hash is of the file's bytes, so uploading the *exact same file* twice is
   refused by the database, not by a check in Python that two parallel
   requests could both pass. It is scoped per user: two users may well
   upload the same file, and that is two separate imports.
2. **Most of the status values are not used yet.** Phase 1 imports
   synchronously inside the request, so an import either finishes
   (`completed`) or the request fails with an error and *no row is written*
   at all - which keeps the file's hash free, so the user can simply try
   again once we fix the parser. `pending`, `processing`, `needs_password`
   and `failed` exist because Phase 2 moves the work into an RQ worker: the
   row is then created first and the client polls it, so a failure has to
   be recorded somewhere. `needs_review` arrives with balance
   reconciliation (SPEC §6.4). The whole enum is here now so that Phase 2
   needs no migration (the same reasoning as ADR 002).
"""

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum_column

# A model may import the (pure) parser layer; the parser layer must never
# import models. `FileType` is defined there because the readers and parsers
# are the code that actually decides what a file is - see app/parsers/base.py.
from app.parsers.base import FileType

# sha256 printed as hex is always 64 characters.
SHA256_HEX_LENGTH = 64
# Long enough for any real name, short enough that a hostile 10 MB "filename"
# cannot be stored. The service truncates to this before inserting.
MAX_FILENAME_LENGTH = 255


class UploadStatus(StrEnum):
    """Where an upload is in its life (SPEC §5). See the module docstring."""

    PENDING = "pending"  # row created, worker has not started (Phase 2)
    PROCESSING = "processing"  # worker is parsing it (Phase 2)
    # An encrypted PDF was uploaded without a password (Phase 2).
    # The S105 silencer below is needed because ruff's flake8-bandit rule
    # flags any constant whose NAME contains "password" as a possible
    # hardcoded secret. This one is a status word - and a real statement
    # password is never stored anywhere (CLAUDE.md rule 4).
    NEEDS_PASSWORD = "needs_password"  # noqa: S105
    NEEDS_REVIEW = "needs_review"  # imported, but balances don't add up (Phase 2)
    COMPLETED = "completed"  # imported successfully
    FAILED = "failed"  # could not be imported (Phase 2)


class StatementUpload(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "statement_uploads"
    __table_args__ = (
        # Dedupe level 1 (SPEC §6.3). It also indexes "this user's uploads",
        # because it starts with user_id.
        UniqueConstraint("user_id", "file_sha256"),
        # Defence in depth: the column can only ever hold lowercase hex of
        # the right length, so a bug that stored something else (a filename,
        # a truncated hash) fails loudly instead of silently breaking dedupe.
        CheckConstraint("file_sha256 ~ '^[0-9a-f]{64}$'", name="file_sha256_hex"),
        # The three counts are counts: they cannot be negative.
        CheckConstraint(
            "rows_parsed >= 0 AND rows_inserted >= 0 AND rows_duplicate >= 0 "
            "AND reconciliation_errors >= 0",
            name="counts_not_negative",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # Which of the user's accounts the statement belongs to. Deleting the
    # account removes its uploads (and, through them, its transactions).
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    # The name the browser sent, kept only so the user recognises the upload
    # in a list. It is user-controlled text, so the service strips any
    # directory part and control characters and truncates it.
    original_filename: Mapped[str] = mapped_column(String(MAX_FILENAME_LENGTH))
    file_type: Mapped[FileType] = mapped_column(str_enum_column(FileType, "file_type"))
    # sha256 of the file's bytes, lowercase hex. NOT the file itself: the
    # bytes are never stored in Phase 1 (SPEC §7.3 - they live in memory for
    # the length of the request and are then gone).
    file_sha256: Mapped[str] = mapped_column(String(SHA256_HEX_LENGTH))
    status: Mapped[UploadStatus] = mapped_column(str_enum_column(UploadStatus, "upload_status"))

    # The period the statement covers, when the file states one (ICICI's
    # header does) or, failing that, the first and last transaction date.
    # NULL only for a statement with no rows and no header period.
    period_start: Mapped[date | None] = mapped_column(Date())
    period_end: Mapped[date | None] = mapped_column(Date())

    # What the import did. rows_parsed = rows_inserted + rows_duplicate.
    rows_parsed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    rows_inserted: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    rows_duplicate: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Rows whose balance column did not add up (SPEC §6.4, Phase 2).
    reconciliation_errors: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # Why it failed, in words safe to show the user: `ParseError` messages
    # never contain narrations, amounts or balances (CLAUDE.md rule 3).
    # Written by the Phase 2 worker; see the module docstring.
    error_message: Mapped[str | None] = mapped_column(Text())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        # IDs and status only: a filename can identify a person, and reprs
        # end up in logs and tracebacks.
        return f"StatementUpload(id={self.id}, status={self.status.value})"
