"""One uploaded statement file and how its import went (SPEC §5 `statement_uploads`).

Every transaction points back to the upload that created it, so this table
answers "where did this row come from?" - and, because `DELETE
/uploads/{id}` (Phase 2) removes the transactions an upload inserted, it is
also the unit of undo.

Two things about the columns are worth knowing before reading them:

1. **A unique index on `(user_id, file_sha256)` is dedupe level 1** (SPEC
   §6.3). The hash is of the file's bytes, so uploading the *exact same
   file* twice is refused by the database, not by a check in Python that
   two parallel requests could both pass. It is scoped per user: two users
   may well upload the same file, and that is two separate imports.

   The index is **partial: it skips `failed` rows** (ADR 011, which
   replaces ADR 008). From Phase 2 a worker parses the file after the
   request has ended, so the row must exist first and a failure has to be
   recorded on it. If a failed row still counted as "already uploaded",
   the user could never retry that file, even after we fixed the parser
   that failed on it.
2. **Some status values are not used yet.** Phase 1 imported synchronously
   inside the request, so only `completed` rows exist so far. Phase 2 moves
   the work into an RQ worker, which brings in `pending`, `processing` and
   `failed`; `needs_password` arrives with password-protected PDFs, and
   `needs_review` with balance reconciliation (SPEC §6.4). The whole enum
   was created up front so that none of this needs a migration (the same
   reasoning as ADR 002).
"""

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
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
# Dedupe level 1 (see the module docstring). Named here because the import
# service needs the name: Postgres reports it when the index rejects an
# INSERT, and that is how a duplicate file is told apart from other errors.
FILE_HASH_INDEX = "uq_statement_uploads_user_id_file_sha256_not_failed"
# Which uploads count as "already uploaded": every one except a failed one
# (ADR 011). Written ONCE, as literal SQL, and used both as the index's
# WHERE clause and in the import service's duplicate check, for two reasons:
#
# - The two must agree, or the friendly 409 and the database disagree on
#   what a duplicate is. One constant means they cannot drift apart (and
#   `alembic check` would not notice if they did: it does not compare an
#   index's WHERE clause).
# - Postgres only uses a partial index when it can PROVE the query's
#   condition implies the index's. With `'failed'` sent as a bound
#   parameter, a prepared ("generic") plan does not know the value, so it
#   cannot prove that, and falls back to a sequential scan. Measured: the
#   bound version cannot use the index even with sequential scans switched
#   off; this literal version does. psycopg prepares a query after it has
#   run 5 times on a connection, so the API would hit this in normal use.
NOT_FAILED_SQL = "status <> 'failed'"
# "This user's uploads", newest first (see the index below).
USER_UPLOADS_INDEX = "ix_statement_uploads_user_id_created_at"


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
        # Dedupe level 1 (SPEC §6.3), as a PARTIAL unique index: rows whose
        # status is `failed` are left out of it, so a failed file can be
        # uploaded again (ADR 011). A plain UNIQUE constraint cannot have a
        # WHERE clause; a unique index can, and it enforces uniqueness in
        # exactly the same way.
        #
        # Being partial, it only covers the non-failed rows, so it cannot
        # serve a query that is about ALL of a user's uploads.
        Index(
            FILE_HASH_INDEX,
            "user_id",
            "file_sha256",
            unique=True,
            postgresql_where=text(NOT_FAILED_SQL),
        ),
        # Every upload of a user, failed ones included, newest first. Two
        # queries need it (migration 0007):
        # - the CASCADE when a user is deleted (`DELETE ... WHERE user_id = ?`
        #   behind DELETE /me); without it that is a sequential scan of
        #   every user's uploads;
        # - the upload list (`GET /uploads`, Phase 2 Step 2), which reads the
        #   index in order and needs no sort step.
        # Named by hand, like the transactions indexes: the naming
        # convention would call it ix_statement_uploads_user_id, which says
        # nothing about the second column.
        Index(USER_UPLOADS_INDEX, "user_id", text("created_at DESC")),
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
    # bytes are kept only in `upload_blobs`, encrypted, until the Phase 2
    # worker has processed them, and then deleted (SPEC §7.3).
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
