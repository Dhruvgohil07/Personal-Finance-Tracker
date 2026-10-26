"""Reading and deleting a user's uploads (GET/DELETE /uploads, SPEC §10).

Creating and processing an upload is the import pipeline, in
`app/services/imports.py`. This module is the simple part: list them, show
one, delete one. Every query is scoped by `user_id` (CLAUDE.md rule 2).
"""

import uuid

import structlog
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.models import StatementUpload, UploadStatus

log = structlog.get_logger()

UPLOAD_NOT_FOUND_MESSAGE = "Upload not found."
UPLOAD_PROCESSING_MESSAGE = (
    "This statement is being imported right now. Try again once it has finished."
)


def list_uploads(db: Session, user_id: uuid.UUID) -> list[StatementUpload]:
    """All of the user's uploads, newest first, failed ones included.

    No pagination (Phase 2 plan): a person uploads a statement or two a
    month, so even years of use is a short list.

    Served by `ix_statement_uploads_user_id_created_at`
    `(user_id, created_at DESC)`, which returns the rows already in this
    order, so Postgres needs no sort step (migration 0007).
    """
    stmt = (
        select(StatementUpload)
        .where(StatementUpload.user_id == user_id)
        .order_by(StatementUpload.created_at.desc())
    )
    return list(db.scalars(stmt))


def get_upload(db: Session, user_id: uuid.UUID, upload_id: uuid.UUID) -> StatementUpload:
    """One of the user's uploads, or NotFoundError (404).

    Someone else's upload is a 404 too, exactly like one that does not
    exist, so ids cannot be probed (IDOR protection, see NotFoundError).
    This is the endpoint a client polls while the worker runs.
    """
    upload = db.scalar(_owned(user_id, upload_id))
    if upload is None:
        raise NotFoundError(UPLOAD_NOT_FOUND_MESSAGE)
    return upload


def delete_upload(db: Session, user_id: uuid.UUID, upload_id: uuid.UUID) -> None:
    """Delete an upload and every transaction it inserted (SPEC §10).

    The undo button for an import that should not have happened: the wrong
    account, the wrong file. The database does the actual work: the
    `transactions.upload_id` and `upload_blobs.upload_id` foreign keys are
    `ON DELETE CASCADE`, so one DELETE of the upload row removes its
    transactions and its stored file in the same transaction.

    Only the transactions this upload INSERTED go. A row that was skipped
    as a duplicate belongs to the earlier upload that inserted it, and
    stays.

    Refused with 409 while the worker is `processing` the upload. The row
    is locked first (`FOR UPDATE`), which waits for a running import to
    commit, so the status read here is final, not a moment-old copy. A
    `pending` upload can be deleted: its job will find nothing and stop.
    """
    upload = db.scalar(_owned(user_id, upload_id).with_for_update())
    if upload is None:
        raise NotFoundError(UPLOAD_NOT_FOUND_MESSAGE)
    if upload.status is UploadStatus.PROCESSING:
        db.rollback()
        raise ConflictError(UPLOAD_PROCESSING_MESSAGE)

    db.delete(upload)
    db.commit()
    log.info("upload_deleted", user_id=str(user_id), upload_id=str(upload_id))


def _owned(user_id: uuid.UUID, upload_id: uuid.UUID) -> Select[tuple[StatementUpload]]:
    """SELECT one upload by id, only if it belongs to `user_id`."""
    return select(StatementUpload).where(
        StatementUpload.id == upload_id, StatementUpload.user_id == user_id
    )
