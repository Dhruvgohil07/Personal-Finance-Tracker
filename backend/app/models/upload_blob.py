"""An uploaded file, encrypted, waiting for the worker (SPEC §5 `upload_blobs`).

From Phase 2, `POST /uploads` stores the file and an RQ worker parses it
later (SPEC §6.1). The API and the worker are separate processes, and on
free hosting they do not share a disk, so the file needs a place both can
reach. Postgres is already that place (ADR 012).

This table is **temporary storage, not an archive**:

- one row per upload, at most. `upload_id` is both the primary key and the
  foreign key, so a second blob for the same upload is impossible;
- the row is deleted as soon as processing finishes or fails, and a
  cleanup job removes anything older than 24 hours (Step 3);
- deleting the upload deletes its blob (`ON DELETE CASCADE`).

The bytes are encrypted by `app/core/crypto.py` (AES-256-GCM) before they
get here, with a key that never enters the database. So this table alone,
in a backup or a leaked dump, holds nothing readable.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, LargeBinary, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.crypto import NONCE_BYTES
from app.db.base import Base


class UploadBlob(Base):
    __tablename__ = "upload_blobs"
    __table_args__ = (
        # Defence in depth, like the sha256 CHECK on statement_uploads: a
        # nonce of any other length is a bug, and it should fail on INSERT
        # rather than as an unreadable file in the worker later.
        CheckConstraint(f"octet_length(nonce) = {NONCE_BYTES}", name="nonce_length"),
    )

    # Primary key AND foreign key: the blob IS a part of its upload, not a
    # separate thing with its own identity, so it needs no id of its own.
    upload_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("statement_uploads.id", ondelete="CASCADE"), primary_key=True
    )
    # `LargeBinary` is Postgres `bytea`: raw bytes, no text encoding. The
    # ciphertext includes GCM's 16-byte authentication tag at the end.
    encrypted_content: Mapped[bytes] = mapped_column(LargeBinary())
    nonce: Mapped[bytes] = mapped_column(LargeBinary())
    # Only created_at, not the usual TimestampMixin: a blob is written once
    # and never updated. The cleanup job reads this to find old blobs.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        # The id only. The content is encrypted, but a repr is no place for
        # file bytes in any form (CLAUDE.md rule 3).
        return f"UploadBlob(upload_id={self.upload_id})"
