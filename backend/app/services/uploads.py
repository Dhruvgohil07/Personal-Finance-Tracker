"""Reading and deleting a user's uploads (GET/DELETE /uploads, SPEC §10).

Creating and processing an upload is the import pipeline, in
`app/services/imports.py`. This module is the simple part: list them, show
one, delete one. Every query is scoped by `user_id` (CLAUDE.md rule 2).
"""

import uuid

import structlog
from rq import Queue
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.models import StatementUpload, Transaction, UploadStatus
from app.services import categorization as categorization_service

log = structlog.get_logger()

UPLOAD_NOT_FOUND_MESSAGE = "Upload not found."
UPLOAD_PROCESSING_MESSAGE = (
    "This statement is being imported right now. Try again once it has finished."
)
OVERLAPPED_BY_LATER_UPLOAD_MESSAGE = (
    "A later statement for this account overlaps this one and relies on its "
    "transactions. Delete that later upload first, then this one."
)

# Statuses of a later upload that may have skipped this upload's rows as
# duplicates: it finished (with or without balance problems), or it is
# being imported right now. A `pending` one has not looked at any row yet,
# and a `failed` one stored nothing.
_RELIES_ON_EARLIER_ROWS = (
    UploadStatus.COMPLETED,
    UploadStatus.NEEDS_REVIEW,
    UploadStatus.PROCESSING,
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
    upload = db.scalar(select_owned_upload(user_id, upload_id))
    if upload is None:
        raise NotFoundError(UPLOAD_NOT_FOUND_MESSAGE)
    return upload


def delete_upload(db: Session, queue: Queue, user_id: uuid.UUID, upload_id: uuid.UUID) -> None:
    """Delete an upload and every transaction it inserted (SPEC §10).

    The undo button for an import that should not have happened: the wrong
    account, the wrong file. The database does the actual work: the
    `transactions.upload_id` and `upload_blobs.upload_id` foreign keys are
    `ON DELETE CASCADE`, so one DELETE of the upload row removes its
    transactions and its stored file in the same transaction.

    Only the transactions this upload INSERTED go. A row that was skipped
    as a duplicate belongs to the earlier upload that inserted it, and
    stays.

    Refused with 409 in two cases:

    - **The upload is being imported.** The worker holds the row's lock for
      the whole import, so the lock is taken with `SKIP LOCKED`: a locked
      row is simply not returned, instead of this request waiting for the
      import to finish (and then deleting it). A row that exists but did
      not come back is therefore busy - 409 at once. A `processing` row
      that nobody holds (a crashed attempt waiting for its retry) is
      refused too. A `pending` upload can be deleted: its job will find
      nothing and stop.
    - **A later upload relies on this one's rows** (`_overlapped_by_later`).
      Deleting the earlier one would silently cut a hole in the later
      statement's period, and that file could not be uploaded again.

    If the upload held half of a detected self transfer, the user's rows
    are re-run once the delete is committed, so the other half is
    re-decided rather than left out of the totals (review of Step 8).
    """
    upload = db.scalar(select_owned_upload(user_id, upload_id).with_for_update(skip_locked=True))
    if upload is None:
        # Not returned: either it is not the user's (404), or it is locked
        # by a running import (409). The plain read does not wait for locks.
        if db.scalar(select_owned_upload(user_id, upload_id)) is not None:
            db.rollback()
            raise ConflictError(UPLOAD_PROCESSING_MESSAGE)
        raise NotFoundError(UPLOAD_NOT_FOUND_MESSAGE)
    if upload.status is UploadStatus.PROCESSING:
        db.rollback()
        raise ConflictError(UPLOAD_PROCESSING_MESSAGE)
    if _overlapped_by_later(db, upload):
        db.rollback()
        raise ConflictError(OVERLAPPED_BY_LATER_UPLOAD_MESSAGE)

    # Asked before the rows are gone (review of Step 8).
    breaks_a_pair = categorization_service.has_detected_self_transfer(
        db, user_id, Transaction.upload_id == upload.id
    )
    db.delete(upload)
    db.commit()
    log.info("upload_deleted", user_id=str(user_id), upload_id=str(upload_id))
    if breaks_a_pair:
        # The other half of a self transfer lost its partner: re-decide it,
        # or it would stay out of the totals for good.
        categorization_service.recategorize_after_commit(db, queue, user_id)


def _overlapped_by_later(db: Session, upload: StatementUpload) -> bool:
    """Does a LATER upload of the same account cover part of this one's period?

    Dedupe skips a row that is already stored (SPEC §6.3), and the skipped
    row stays attached to the upload that inserted it. So when a later
    statement overlaps this one, its rows in the overlap were most likely
    skipped as duplicates of THIS upload's - and deleting this upload would
    delete them, even though the later statement contains them too.

    The test is deliberately simple and errs on the side of refusing: same
    account, created later, periods overlap, and the later upload reported
    duplicates. It cannot tell which earlier upload those duplicates
    matched; nothing records that per row. Refusing costs the user one
    extra step (delete the later one first); allowing it would lose data
    without a word.

    An upload that inserted nothing has nothing to protect.
    """
    if upload.rows_inserted == 0 or upload.period_start is None or upload.period_end is None:
        return False
    stmt = select(StatementUpload.id).where(
        StatementUpload.user_id == upload.user_id,
        StatementUpload.account_id == upload.account_id,
        StatementUpload.id != upload.id,
        StatementUpload.created_at > upload.created_at,
        StatementUpload.status.in_(_RELIES_ON_EARLIER_ROWS),
        StatementUpload.rows_duplicate > 0,
        StatementUpload.period_start <= upload.period_end,
        StatementUpload.period_end >= upload.period_start,
    )
    return db.scalar(stmt.limit(1)) is not None


def select_owned_upload(user_id: uuid.UUID, upload_id: uuid.UUID) -> Select[tuple[StatementUpload]]:
    """SELECT one upload by id, only if it belongs to `user_id`.

    The one definition of "this user's upload" (CLAUDE.md rule 2): GET and
    DELETE here, and POST /uploads/{id}/password in the import service, all
    build on it, adding a lock where they need one.
    """
    return select(StatementUpload).where(
        StatementUpload.id == upload_id, StatementUpload.user_id == user_id
    )
