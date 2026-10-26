"""The hourly cleanup (SPEC §11 `cleanup()`, Phase 2 Step 3).

Three things that nothing else would ever tidy up:

1. **Stale uploads.** An upload still `pending` or `processing` an hour
   after it was created has no job working on it any more: the API died
   between commit and enqueue, or the worker never came back. Left alone,
   it would sit there forever, and the user's polling would never end. It
   is marked `failed` (which also frees the file for a new upload, ADR 011)
   and its stored file is deleted.
2. **Old stored files** (`upload_blobs`). A file is kept only until its
   import is over (SPEC §7.3). Anything older than 24 hours is deleted, and
   an upload still waiting for it is failed as expired. In practice that is
   a password-protected PDF whose password never came (`needs_password`,
   Step 10): the stale rule leaves that status alone, because waiting for
   the user is not a lost job.
3. **Dead refresh-token families.** See `_delete_dead_token_families`: a
   token is deleted only once its whole family has expired, so reuse
   detection keeps working.

Each rule is ONE set-based statement (`UPDATE ... WHERE ... RETURNING`,
`DELETE ... WHERE ...`), not a loop over rows. Besides being fewer round
trips, that is what makes the rules safe next to a running import: if the
worker holds an upload's row lock, the UPDATE waits for it and then checks
its WHERE clause again against the row as the worker left it (Postgres
does this in READ COMMITTED). An upload the worker just completed no longer
matches `status IN ('pending', 'processing')`, so it is not failed.

Everything happens in one transaction and is safe to run again: a second
run finds nothing left to do. That matters, because the scheduler will run
it every hour no matter what happened the hour before.

Like the other services, this knows nothing about RQ; the job in
app/workers/jobs.py gives it a session and the time.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.models import RefreshToken, StatementUpload, UploadBlob, UploadStatus
from app.services.imports import STALE_UPLOAD_AFTER, STALE_UPLOAD_MESSAGE, UNFINISHED_STATUSES

log = structlog.get_logger()

# SPEC §5 `upload_blobs`: "a cleanup job also deletes any blob older than 24h".
BLOB_MAX_AGE = timedelta(hours=24)
UPLOAD_EXPIRED_MESSAGE = "This upload expired before it could be imported. Please upload it again."

# Uploads that may still be waiting for their stored file. Everything else
# (completed, needs_review, failed) is final and has no use for one.
_WAITING_FOR_FILE = (*UNFINISHED_STATUSES, UploadStatus.NEEDS_PASSWORD)


@dataclass(frozen=True)
class CleanupCounts:
    """What one run did. All zero on a run that found nothing to do."""

    stale_uploads_failed: int
    expired_uploads_failed: int
    blobs_deleted: int
    refresh_tokens_deleted: int


def run_cleanup(db: Session, *, now: datetime) -> CleanupCounts:
    """Apply the three rules (see the module docstring) and commit.

    `now` is passed in rather than read here, so a test can say exactly
    what time it is; the job passes the real time. The rules run in this
    order on purpose: the stale rule deletes the files of the uploads it
    fails, so the file rule only finds what is left.
    """
    stale_ids, stale_blobs = _fail_stale_uploads(db, now)
    expired_ids, old_blobs = _expire_old_files(db, now)
    tokens = _delete_dead_token_families(db, now)
    db.commit()

    counts = CleanupCounts(
        stale_uploads_failed=len(stale_ids),
        expired_uploads_failed=len(expired_ids),
        blobs_deleted=stale_blobs + old_blobs,
        refresh_tokens_deleted=tokens,
    )
    # Counts only. The ids of the failed uploads are not logged: there may
    # be many, and each one is already visible as `failed` in the database.
    log.info(
        "cleanup_finished",
        stale_uploads_failed=counts.stale_uploads_failed,
        expired_uploads_failed=counts.expired_uploads_failed,
        blobs_deleted=counts.blobs_deleted,
        # Not "refresh_tokens_deleted": the log redaction hides every key
        # containing "token" (app/core/logging.py), and would hide the count.
        refresh_rows_deleted=counts.refresh_tokens_deleted,
    )
    return counts


def _fail_stale_uploads(db: Session, now: datetime) -> tuple[list[uuid.UUID], int]:
    """Rule 1: fail `pending`/`processing` uploads older than STALE_UPLOAD_AFTER.

    Returns the ids it failed and the number of stored files it deleted.
    The threshold is the same one `create_upload` uses when a user uploads
    the same file again, so the two can never disagree about "stale".
    """
    failed_ids = list(
        db.scalars(
            update(StatementUpload)
            .where(
                StatementUpload.status.in_(UNFINISHED_STATUSES),
                StatementUpload.created_at < now - STALE_UPLOAD_AFTER,
            )
            .values(status=UploadStatus.FAILED, error_message=STALE_UPLOAD_MESSAGE)
            .returning(StatementUpload.id)
            # The ORM must not try to apply this UPDATE to objects already
            # loaded in the session; there are none, and it is set-based.
            .execution_options(synchronize_session=False)
        )
    )
    if not failed_ids:
        return failed_ids, 0
    deleted = db.execute(
        delete(UploadBlob)
        .where(UploadBlob.upload_id.in_(failed_ids))
        .returning(UploadBlob.upload_id)
    )
    return failed_ids, len(deleted.all())


def _expire_old_files(db: Session, now: datetime) -> tuple[list[uuid.UUID], int]:
    """Rule 2: delete stored files older than BLOB_MAX_AGE; fail who waited for them.

    Returns the ids of the uploads it failed and the number of files deleted.
    An old file whose upload is already final (which should not happen, but
    a crash at the wrong moment could leave one) is deleted all the same.
    """
    cutoff = now - BLOB_MAX_AGE
    old_files = select(UploadBlob.upload_id).where(UploadBlob.created_at < cutoff)
    failed_ids = list(
        db.scalars(
            update(StatementUpload)
            .where(StatementUpload.id.in_(old_files), StatementUpload.status.in_(_WAITING_FOR_FILE))
            .values(status=UploadStatus.FAILED, error_message=UPLOAD_EXPIRED_MESSAGE)
            .returning(StatementUpload.id)
            .execution_options(synchronize_session=False)
        )
    )
    deleted = db.execute(
        delete(UploadBlob).where(UploadBlob.created_at < cutoff).returning(UploadBlob.upload_id)
    )
    return failed_ids, len(deleted.all())


def _delete_dead_token_families(db: Session, now: datetime) -> int:
    """Rule 3: delete refresh tokens whose WHOLE family has expired.

    Why not simply every expired token? Because a revoked token is how a
    stolen token is caught (app/services/auth.py, reuse detection). Picture
    a thief who refreshed with a stolen token T1 and now holds T2. The real
    user comes back with T1 weeks later, after T1 itself has expired. With
    T1 still stored, the replay is recognised and the whole family -
    including the thief's T2 - is revoked. With T1 deleted, it would just be
    an unknown token (401), and the thief would keep their session.

    So a family is kept while ANY of its tokens is still valid: as long as
    someone could refresh in it, its old tokens are evidence. Once the
    newest token has expired too, nobody can refresh in the family any
    more, the evidence protects nothing, and the whole family goes.
    Returns the number of tokens deleted.
    """
    dead_families = (
        select(RefreshToken.family_id)
        .group_by(RefreshToken.family_id)
        .having(func.max(RefreshToken.expires_at) < now)
    )
    deleted = db.execute(
        delete(RefreshToken)
        .where(RefreshToken.family_id.in_(dead_families))
        .returning(RefreshToken.id)
    )
    return len(deleted.all())
