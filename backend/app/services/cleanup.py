"""The hourly cleanup (SPEC §11 `cleanup()`, Phase 2 Step 3).

Three things that nothing else would ever tidy up:

1. **Stale uploads.** An upload still `pending` or `processing` an hour
   after it was created, with no job of it left in Redis, will never
   finish: the API died between commit and enqueue, or the job was lost.
   Left alone, it would sit there forever and the user's polling would
   never end. It is marked `failed` (which also frees the file for a new
   upload, ADR 011) and its stored file is deleted. The rule itself lives
   in app/services/imports.py (`fail_stale_uploads`), because re-uploading
   the same file applies it too.
2. **Old stored files** (`upload_blobs`). A file is kept only until its
   import is over (SPEC §7.3). Anything older than 24 hours is deleted, and
   an upload still waiting for it is failed as expired. In practice that is
   a password-protected PDF whose password never came (`needs_password`,
   Step 10): the stale rule leaves that status alone, because waiting for
   the user is not a lost job.
3. **Dead refresh-token families.** See `_delete_dead_token_families`: a
   token is deleted only once its whole family has expired, so reuse
   detection keeps working.

Each rule is a set-based statement (`UPDATE ... WHERE ... RETURNING`,
`DELETE ... WHERE ...`), not a loop over rows, which is also what makes
them safe next to a running import (see `fail_uploads_where`).

**Each rule commits on its own.** One transaction for all three would hold
the row locks of the uploads failed by rule 1 until rule 3 was done too -
and a locked upload looks "busy" to `DELETE /uploads/{id}` (it answers
"being imported right now") and makes a re-upload of the same file wait.
Committing per rule keeps every lock short. Nothing is lost by it: every
rule is safe to run again, so if the job dies between two rules, the next
run simply does the rest. The scheduler runs it every hour regardless.

Like the other services, this knows nothing about RQ: the job in
app/workers/jobs.py gives it a session, the time, and the function that
asks Redis whether an upload's job is still waiting.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from sqlalchemy import and_, delete, select
from sqlalchemy.orm import Session, aliased

from app.models import RefreshToken, StatementUpload, UploadBlob
from app.services.imports import (
    NOT_FINAL_STATUSES,
    JobIsWaiting,
    fail_stale_uploads,
    fail_uploads_where,
)

log = structlog.get_logger()

# SPEC §5 `upload_blobs`: "a cleanup job also deletes any blob older than 24h".
BLOB_MAX_AGE = timedelta(hours=24)
UPLOAD_EXPIRED_MESSAGE = "This upload expired before it could be imported. Please upload it again."

# A token family counts as dead only once its newest token expired at least
# this long ago. Without the margin, a refresh checked a moment before the
# newest token expired could commit a new token into a family this run is
# deleting (see `_delete_dead_token_families`). No request takes an hour.
DEAD_FAMILY_GRACE = timedelta(hours=1)

# Uploads that may still be waiting for their stored file: the ones not
# final yet (pending, processing, needs_password). Everything else
# (completed, needs_review, failed) is final and has no use for one.
_WAITING_FOR_FILE = NOT_FINAL_STATUSES


@dataclass(frozen=True)
class CleanupCounts:
    """What one run did. All zero on a run that found nothing to do."""

    stale_uploads_failed: int
    expired_uploads_failed: int
    blobs_deleted: int
    refresh_tokens_deleted: int


def run_cleanup(db: Session, *, now: datetime, job_is_waiting: JobIsWaiting) -> CleanupCounts:
    """Apply the three rules (see the module docstring), committing after each.

    `now` is passed in rather than read here, so a test can say exactly
    what time it is; the job passes the real time. `job_is_waiting` answers
    "is this upload's job still in Redis?" (the job passes
    `statement_job_waiting_check`). The order matters: the stale rule
    deletes the files of the uploads it fails, so the file rule only finds
    what is left.
    """
    stale_ids, stale_files = fail_stale_uploads(db, now=now, job_is_waiting=job_is_waiting)
    db.commit()

    expired_ids, expired_files = _expire_old_files(db, now)
    db.commit()

    tokens = _delete_dead_token_families(db, now)
    db.commit()

    counts = CleanupCounts(
        stale_uploads_failed=len(stale_ids),
        expired_uploads_failed=len(expired_ids),
        blobs_deleted=stale_files + expired_files,
        refresh_tokens_deleted=tokens,
    )
    # Counts only. The ids of the failed uploads are not logged: there may
    # be many, and each one is already visible as `failed` in the database.
    # (A count may have "token" in its name: the redaction lets integer
    # counts through, see app/core/logging.py.)
    log.info(
        "cleanup_finished",
        stale_uploads_failed=counts.stale_uploads_failed,
        expired_uploads_failed=counts.expired_uploads_failed,
        blobs_deleted=counts.blobs_deleted,
        refresh_tokens_deleted=counts.refresh_tokens_deleted,
    )
    return counts


def _expire_old_files(db: Session, now: datetime) -> tuple[list[uuid.UUID], int]:
    """Rule 2: delete stored files older than BLOB_MAX_AGE; fail who waited for them.

    Returns the ids of the uploads it failed and the number of files deleted.
    An old file whose upload is already final (which should not happen, but
    a crash at the wrong moment could leave one) is deleted all the same.
    """
    cutoff = now - BLOB_MAX_AGE
    old_files = select(UploadBlob.upload_id).where(UploadBlob.created_at < cutoff)
    failed_ids, failed_files = fail_uploads_where(
        db,
        and_(StatementUpload.id.in_(old_files), StatementUpload.status.in_(_WAITING_FOR_FILE)),
        UPLOAD_EXPIRED_MESSAGE,
    )
    leftovers = db.execute(
        delete(UploadBlob).where(UploadBlob.created_at < cutoff).returning(UploadBlob.upload_id)
    )
    return failed_ids, failed_files + len(leftovers.all())


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
    newest token has expired too (plus DEAD_FAMILY_GRACE), the whole family
    goes. Returns the number of tokens deleted.

    The statement reads only rows that can actually be deleted:

        DELETE FROM refresh_tokens t
        WHERE t.expires_at < :cutoff                      -- index on expires_at
          AND NOT EXISTS (SELECT 1 FROM refresh_tokens u  -- index on family_id
                          WHERE u.family_id = t.family_id
                            AND u.expires_at >= :cutoff)

    Every token of a dead family is itself older than the cutoff, so "the
    expired rows whose family has no live token" is exactly "all rows of
    the dead families". The grace margin closes a race: without it, a
    refresh that validated the newest token just before it expired could
    commit a new token, invisible to this DELETE's snapshot, into a family
    whose old rows (the evidence) are being deleted.
    """
    cutoff = now - DEAD_FAMILY_GRACE
    sibling = aliased(RefreshToken)
    live_sibling = select(sibling.id).where(
        sibling.family_id == RefreshToken.family_id, sibling.expires_at >= cutoff
    )
    deleted = db.execute(
        delete(RefreshToken)
        .where(RefreshToken.expires_at < cutoff, ~live_sibling.exists())
        .returning(RefreshToken.id)
    )
    return len(deleted.all())
