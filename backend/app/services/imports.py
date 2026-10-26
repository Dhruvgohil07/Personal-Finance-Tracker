"""Turn an uploaded statement file into transactions (SPEC §6.1).

This is the pipeline the whole project exists for. Since Phase 2 it runs
in two halves, in two different processes (ADR 013):

    API request: POST /uploads              RQ worker: process_statement
    --------------------------              ----------------------------
    create_upload()                         process_upload()
      size, account, sha256, file type        status -> processing
      encrypt the file -> upload_blobs        decrypt the file
      statement_uploads row: `pending`        parse_statement()
      commit, then enqueue(upload_id)         does it match the account?
      -> 202 Accepted                         store_transactions()
                                              categorize_upload()
                                              delete the file -> completed

The client then polls `GET /uploads/{id}` until the status is final.

Why split it: parsing a year of transactions is the slowest thing the app
does, and in Phase 2 it gets slower (PDFs, reconciliation,
categorization). A request should not wait for it, and a worker can retry
when the database blips, which a request cannot.

**What is checked where.** The request does every check that is cheap and
needs no parsing, so the most common mistakes still get an immediate
answer: too large (413), not your account (404), the same file again
(409), not a statement file at all (400). Everything that needs the file
parsed - an unreadable row, a layout no parser knows, a statement for a
different account - is found by the worker and recorded on the upload as
`failed`, with the reason in `error_message`.

**Failures** (ADR 011, ADR 013). A failed upload keeps its row, with the
reason, and the file-hash index skips `failed` rows, so the user can
upload the same file again at once.
"""

import dataclasses
import hashlib
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import PurePosixPath
from typing import NoReturn

import structlog
from redis.exceptions import RedisError
from rq import Queue
from sqlalchemy import ColumnElement, and_, delete, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.crypto import EncryptedFile, FileDecryptionError, decrypt_file, encrypt_file
from app.core.errors import (
    ConflictError,
    InvalidStatementError,
    PayloadTooLargeError,
    ServiceUnavailableError,
)
from app.db.errors import violates_constraint
from app.models import Account, StatementUpload, Transaction, UploadBlob, UploadStatus, User
from app.models.statement_upload import FILE_HASH_INDEX, MAX_FILENAME_LENGTH, NOT_FAILED_SQL
from app.parsers.base import FileType, ParsedStatement, ParseError, RawRow, StatementParser
from app.parsers.fingerprint import fingerprints_for
from app.parsers.generic_csv import ColumnMapping, GenericCsvParser
from app.parsers.normalize import normalize_row
from app.parsers.readers import READABLE_FILE_TYPES, detect_file_type, read_statement_as
from app.parsers.reconcile import Issue, reconcile, to_chronological
from app.parsers.registry import ParserRegistry, build_default_registry
from app.services import accounts as accounts_service
from app.services import categorization as categorization_service
from app.workers.queue import enqueue_statement_processing, statement_job_waiting_check

log = structlog.get_logger()

DUPLICATE_FILE_MESSAGE = "You have already uploaded this file."
# A duplicate that has not finished importing yet. Saying "already
# uploaded" would be wrong: nothing from it is imported so far. The 409's
# `details` carry its upload id, so the client can poll that upload.
FILE_BEING_IMPORTED_MESSAGE = (
    "This file is already being imported. Check that upload for the result."
)
NO_PARSER_MESSAGE = (
    "We could not recognise this statement's layout. If it is a CSV, send a "
    "column_mapping describing which column holds the date, the description "
    "and the amount."
)
MAPPING_NEEDS_CSV_MESSAGE = "A column mapping can only be used with a CSV file."
WRONG_ACCOUNT_NUMBER_MESSAGE = (
    "This statement is for a different account number than the account you selected."
)
# For a detected type the readers cannot read yet (`READABLE_FILE_TYPES`).
# Today that is only PDF, until the PDF parser exists (Phase 2 Step 9). The
# type is known from the signature, so there is no reason to store the file
# and let the worker discover the same thing.
PDF_NOT_SUPPORTED_MESSAGE = "PDF statements are not supported yet; upload the .xls or CSV version."

# The `error_message` of a failed upload, for the failures that are not the
# file's fault. Each one tells the user what to do next.
QUEUE_UNAVAILABLE_MESSAGE = (
    "We could not start importing this file. Please upload it again in a few minutes."
)
FILE_MISSING_MESSAGE = "The uploaded file is no longer available. Please upload it again."
FILE_UNREADABLE_MESSAGE = "The stored file could not be read. Please upload it again."
UNEXPECTED_FAILURE_MESSAGE = (
    "Something went wrong while importing this statement. Please upload it again."
)
STALE_UPLOAD_MESSAGE = "This import did not finish. Please upload the file again."

# How long an upload may stay `pending`/`processing` before it is presumed
# lost: the API died between commit and enqueue, or its job is gone. A real
# import takes seconds; RQ's retries add at most ~6 minutes. Age alone is
# not enough, though: see `fail_stale_uploads`.
STALE_UPLOAD_AFTER = timedelta(hours=1)

# "Is this upload's job still waiting in Redis (queued, scheduled, running)?"
# Given to `fail_stale_uploads` by its caller; see
# `statement_job_waiting_check` in app/workers/queue.py.
JobIsWaiting = Callable[[uuid.UUID], bool]

# The statuses in which an upload still has work to do. `processing` is in
# it because a worker that crashed half-way leaves the row `processing`,
# and RQ's retry must be able to pick it up again. Public: the upload routes
# (Retry-After) and the cleanup job use the same definition.
UNFINISHED_STATUSES = (UploadStatus.PENDING, UploadStatus.PROCESSING)

# At most this many reconciliation issues are STORED on an upload (the
# count in `reconciliation_errors` is always the full number). A statement
# the parser misread completely can produce one issue per row; a hundred
# are plenty to see the pattern, and the JSON stays small.
MAX_STORED_ISSUES = 100

# How many transaction rows go into one INSERT statement.
#
# Not a performance tweak - a correctness limit. Postgres accepts at most
# 65535 bind parameters per statement, and each row here binds about 20
# (one per column), so a single INSERT caps out at roughly 3200 rows. A
# two-year statement would exceed that and fail with an error that says
# nothing about the real cause. All the chunks still run inside the SAME
# transaction, so the import stays all-or-nothing.
#
# Crossing this boundary is covered by
# `test_a_statement_larger_than_one_chunk_is_fully_imported`, which reads
# the number from here - so tuning it keeps the test honest.
_INSERT_CHUNK_SIZE = 500


@dataclass(frozen=True)
class ImportCounts:
    """What one import did. `rows_parsed == rows_inserted + rows_duplicate`."""

    rows_parsed: int
    rows_inserted: int
    rows_duplicate: int


@lru_cache(maxsize=1)
def _registry() -> ParserRegistry:
    """The bank parsers, built once per process.

    `build_default_registry()` imports xlrd (and pdfplumber in Phase 2), so
    building it on every upload would do that work again and again.
    `lru_cache` is the standard-library way to say "compute this once".
    """
    return build_default_registry()


# --- API side: store the file and hand it to the worker -------------------


def create_upload(
    db: Session,
    user_id: uuid.UUID,
    *,
    account_id: uuid.UUID,
    filename: str,
    content: bytes,
    queue: Queue,
    column_mapping: ColumnMapping | None = None,
) -> StatementUpload:
    """Store an uploaded file and queue it for import; return the `pending` row.

    Raises (all of which become one of our standard error responses):

    - `PayloadTooLargeError` (413)   - over `MAX_UPLOAD_MB`
    - `NotFoundError` (404)          - unknown account, or someone else's
    - `ConflictError` (409)          - this exact file was already uploaded,
                                       or is still being imported
    - `InvalidStatementError` (400)  - not a file type we can read
    - `ServiceUnavailableError` (503) - the queue (Redis) is unreachable

    Order of the checks, cheapest first, and nothing parsed:

    1. **size**, before anything else (SPEC §6.1). The route stops reading
       at the limit already; this is the backstop for any other caller.
    2. **account ownership**, via `accounts_service.get_account`, which
       answers 404 for an account that does not exist *or* belongs to
       someone else (rule 2, IDOR).
    3. **sha256 of the bytes** -> was this exact file already uploaded by
       this user? Dedupe level 1 (SPEC §6.3). An earlier upload of it that
       is stale (`fail_stale_uploads`: unfinished for over an hour, and no
       job waiting for it) is failed first, so a lost job cannot block the
       file.
    4. **file type, from the file's signature** ("magic bytes"), so a
       Word document renamed to `.xls` is refused here, not in the worker.

    Then the row and the encrypted file are committed together, and only
    then is the job enqueued: the worker must be able to find both the
    moment it starts, which can be before this function returns.
    """
    settings = get_settings()
    if len(content) > settings.max_upload_bytes:
        raise PayloadTooLargeError(
            f"The file is larger than the {settings.max_upload_mb} MB upload limit."
        )

    account = accounts_service.get_account(db, user_id, account_id)

    file_sha256 = hashlib.sha256(content).hexdigest()
    existing = _live_upload(db, user_id, file_sha256)
    if existing is not None and existing.status in UNFINISHED_STATUSES:
        # Fail it if it is stale - the same rule, in the same SQL, as the
        # hourly cleanup - and then LOOK AGAIN rather than trusting what
        # this request did. The cleanup job or the worker may have failed
        # it a moment earlier (this UPDATE then waits for their lock and
        # matches nothing), and either way the file is free.
        failed, _files = fail_stale_uploads(
            db,
            now=datetime.now(UTC),
            job_is_waiting=statement_job_waiting_check(queue.connection),
            upload_id=existing.id,
        )
        db.commit()
        if failed:
            log.info(
                "statement_import_failed",
                user_id=str(user_id),
                upload_id=str(existing.id),
                reason="stale",
            )
        existing = _live_upload(db, user_id, file_sha256)
    if existing is not None:
        _raise_duplicate(existing)

    file_type = _supported_file_type(filename, content)

    upload = StatementUpload(
        user_id=user_id,
        account_id=account.id,
        original_filename=_safe_filename(filename),
        file_type=file_type,
        file_sha256=file_sha256,
        status=UploadStatus.PENDING,
        column_mapping=_mapping_to_json(column_mapping),
    )
    db.add(upload)
    try:
        # flush() sends the INSERT without committing. The row's id is
        # needed first: it is bound into the encryption (associated data),
        # so the blob can only ever be decrypted for this upload.
        db.flush()
        encrypted = encrypt_file(content, upload.id)
        db.add(
            UploadBlob(
                upload_id=upload.id,
                encrypted_content=encrypted.ciphertext,
                nonce=encrypted.nonce,
            )
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # The check above already answers 409 for a file we have seen. This
        # is the race: two uploads of the same file at the same time, where
        # both passed the check and the database rejected the second.
        # For a unique INDEX (not a constraint) Postgres reports the
        # index's name in the same field, so the same check works.
        if violates_constraint(exc, FILE_HASH_INDEX):
            raise ConflictError(DUPLICATE_FILE_MESSAGE) from None
        raise

    try:
        enqueue_statement_processing(queue, upload.id)
    except RedisError:
        # Most likely no job exists, and the committed row would stay
        # `pending` forever, blocking the same file (only `failed` frees
        # it). So it is failed right away, which also deletes the file, and
        # the user is told to try again.
        #
        # But an error can also arrive AFTER Redis accepted the job: a
        # reply that times out. A fast worker may then already have claimed
        # or even finished the upload. `fail_upload` only fails an upload
        # that is still `pending`, and says whether it did. If it did not,
        # the job got through, and the upload is answered as accepted.
        if fail_upload(db, upload.id, QUEUE_UNAVAILABLE_MESSAGE, reason="queue_unavailable"):
            raise ServiceUnavailableError(QUEUE_UNAVAILABLE_MESSAGE) from None
        log.warning("statement_upload_enqueue_error_after_job_ran", upload_id=str(upload.id))

    # IDs and the file type only: no filename (it can identify a person),
    # nothing from the file (CLAUDE.md rule 3).
    log.info(
        "statement_upload_queued",
        user_id=str(user_id),
        account_id=str(account.id),
        upload_id=str(upload.id),
        file_type=file_type.value,
    )
    return upload


# --- Worker side: parse the stored file and import it ---------------------


def process_upload(db: Session, upload_id: uuid.UUID) -> None:
    """Import a stored upload. Called by the `process_statement` job.

    Two transactions, on purpose:

    1. **Claim** the upload: lock its row, check that it still needs work,
       set `processing`, commit. Committing straight away is what makes
       `processing` visible to `GET /uploads/{id}`, and what makes
       `DELETE /uploads/{id}` refuse while the import runs.
    2. **Import**, all-or-nothing: lock the row again, read and decrypt the
       file, parse, insert the transactions, delete the file, set
       `completed`, commit. A crash anywhere in here leaves no
       transactions behind and the row `processing`, so a retry starts
       again from a clean state.

    **Idempotent** (SPEC §11): an upload that is already `completed` or
    `failed`, or that no longer exists (deleted by the user), is skipped,
    so running the job twice changes nothing.

    The row lock (`SELECT ... FOR UPDATE`) covers a job that was enqueued
    twice: the second run waits for the first to commit, then finds the
    upload finished and does nothing.

    **Lock order.** Transaction 2 first takes `FOR KEY SHARE` on the user
    and the account rows, and only then the upload row. Inserting the
    transactions needs those two locks anyway (their foreign keys), and
    `DELETE /accounts/{id}` or `DELETE /me` take them in the same order:
    the owner row first, then (by cascade) the upload. Taking the upload
    first would make an import and such a delete wait for each other
    (a deadlock, and a 500 for the user). Same order, no cycle: the delete
    simply waits until the import has committed.

    An expected failure - an unreadable file, a statement for a different
    account, a missing or damaged blob - is recorded with `fail_upload` and
    the function returns normally, so RQ does not retry it. Anything else
    is raised for RQ to retry.
    """
    claimed = _claim(db, upload_id)
    if claimed is None:
        return

    # Transaction 2, locks in the fixed order: owners first (see above).
    _lock_owners(db, claimed.user_id, claimed.account_id)
    # `populate_existing` re-reads the row even though the session already
    # has it, so the status checked below is the one in the database now,
    # after taking the lock - not the one from step 1.
    upload = db.get(StatementUpload, upload_id, with_for_update=True, populate_existing=True)
    if upload is None or upload.status is not UploadStatus.PROCESSING:
        # Another run of the same job finished it (or the user deleted it)
        # while this one waited for the lock.
        db.rollback()
        log.info("statement_import_skipped", upload_id=str(upload_id))
        return

    blob = db.get(UploadBlob, upload_id)
    if blob is None:
        # Normally impossible: the blob is written with the row and only
        # deleted together with a final status. The Step 3 cleanup job
        # deletes blobs older than 24 hours, though, so a job that waited
        # in the queue that long would find nothing to read.
        db.rollback()
        fail_upload(db, upload_id, FILE_MISSING_MESSAGE, reason="file_missing")
        return

    try:
        content = decrypt_file(
            EncryptedFile(nonce=blob.nonce, ciphertext=blob.encrypted_content), upload_id
        )
        # By the type the request decided, not by detecting it again: the
        # stored filename may be cut short (255 characters), and for a CSV
        # the name is the only sign of what it is.
        statement = parse_statement(
            upload.file_type, content, column_mapping=_mapping_from_json(upload.column_mapping)
        )
        account = db.get_one(Account, upload.account_id)
        _check_statement_matches_account(account, statement)
    except FileDecryptionError:
        db.rollback()
        fail_upload(db, upload_id, FILE_UNREADABLE_MESSAGE, reason="file_unreadable")
        return
    except InvalidStatementError as exc:
        db.rollback()
        # The message is the parser's own, written to be shown to the user:
        # what went wrong and in which row, never the row's content.
        fail_upload(db, upload_id, exc.message, reason="invalid_statement")
        return

    # Oldest first, then checked (SPEC §6.4, ADR 014). The check runs on
    # the rows as the bank printed them - in memory, before anything is
    # stored - and never stops the import: a statement that does not add
    # up is still imported, but marked for the user to review.
    rows = to_chronological(statement.rows)
    issues = reconcile(
        rows,
        opening_balance_paise=statement.opening_balance_paise,
        closing_balance_paise=statement.closing_balance_paise,
    )

    upload.period_start, upload.period_end = _statement_period(statement)
    counts = store_transactions(db, upload, rows)
    # Same transaction as the insert, so rows appear already categorized
    # (SPEC §8, ADR 016). The INSERT has already run inside this (still
    # uncommitted) transaction, so the categorizer's SELECT sees the rows.
    rows_categorized = categorization_service.categorize_upload(db, upload)
    upload.rows_parsed = counts.rows_parsed
    upload.rows_inserted = counts.rows_inserted
    upload.rows_duplicate = counts.rows_duplicate
    upload.reconciliation_errors = len(issues or [])
    upload.reconciliation_issues = _issues_to_json(issues)
    # The file has served its purpose: it is deleted in the same commit
    # that stores its transactions (SPEC §7.3).
    db.delete(blob)
    upload.status = UploadStatus.NEEDS_REVIEW if issues else UploadStatus.COMPLETED
    upload.completed_at = datetime.now(UTC)
    db.commit()

    # IDs, the file type and counts only: no filename, no narration, no
    # amounts, no balances (CLAUDE.md rule 3).
    log.info(
        "statement_imported",
        user_id=str(upload.user_id),
        account_id=str(upload.account_id),
        upload_id=str(upload.id),
        file_type=upload.file_type.value,
        rows_parsed=counts.rows_parsed,
        rows_inserted=counts.rows_inserted,
        rows_duplicate=counts.rows_duplicate,
        rows_categorized=rows_categorized,
        reconciliation_errors=upload.reconciliation_errors,
        status=upload.status.value,
    )


def fail_upload(db: Session, upload_id: uuid.UUID, message: str, *, reason: str) -> bool:
    """Mark an unfinished upload `failed`, delete its stored file, and commit.

    Returns True if it did, False if the upload was already final or gone.

    `message` is shown to the user (`error_message`), so it must be safe to
    show: no narrations, amounts or balances. `reason` is a short fixed
    code for the log, which never gets the message itself.

    Only an unfinished (`pending`/`processing`) upload is changed. One that
    is already final, or gone, is left alone - the job's failure callback
    can arrive after the upload was finished some other way, and must not
    turn a `completed` import into a `failed` one.

    Setting `failed` also frees the file: the file-hash index skips failed
    rows, so the user can upload the same file again (ADR 011).
    """
    upload = db.get(StatementUpload, upload_id, with_for_update=True, populate_existing=True)
    if upload is None or upload.status not in UNFINISHED_STATUSES:
        db.rollback()
        return False

    upload.status = UploadStatus.FAILED
    upload.error_message = message
    db.execute(delete(UploadBlob).where(UploadBlob.upload_id == upload_id))
    db.commit()
    log.info(
        "statement_import_failed",
        user_id=str(upload.user_id),
        upload_id=str(upload_id),
        reason=reason,
    )
    return True


def stale_upload_condition(now: datetime) -> ColumnElement[bool]:
    """SQL condition: unfinished, and created more than STALE_UPLOAD_AFTER ago.

    The one definition of "stale by age", used by the hourly cleanup and by
    `create_upload` alike, so the two cannot disagree.
    """
    return and_(
        StatementUpload.status.in_(UNFINISHED_STATUSES),
        StatementUpload.created_at < now - STALE_UPLOAD_AFTER,
    )


def fail_uploads_where(
    db: Session, condition: ColumnElement[bool], message: str
) -> tuple[list[uuid.UUID], int]:
    """Fail every upload matching `condition` and delete their stored files.

    The set-based form of `fail_upload`: one `UPDATE ... RETURNING`, then
    one DELETE of the files. Returns (failed ids, files deleted). Does NOT
    commit; the caller decides when.

    Safe next to a running import: if the worker holds an upload's row
    lock, the UPDATE waits for it and then checks `condition` again against
    the row as the worker left it (Postgres does this in READ COMMITTED),
    so an upload that has just completed no longer matches.
    """
    failed_ids = list(
        db.scalars(
            update(StatementUpload)
            .where(condition)
            .values(status=UploadStatus.FAILED, error_message=message)
            .returning(StatementUpload.id)
            # Set-based: the ORM must not try to apply this to objects
            # loaded in the session (`_live_upload` re-reads with
            # populate_existing instead).
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


def fail_stale_uploads(
    db: Session,
    *,
    now: datetime,
    job_is_waiting: JobIsWaiting,
    upload_id: uuid.UUID | None = None,
) -> tuple[list[uuid.UUID], int]:
    """Fail stale uploads (all of them, or just `upload_id`); no commit.

    Stale = unfinished for over an hour (`stale_upload_condition`) AND no
    job of it still waiting in Redis. Age alone would be wrong after
    downtime: the uploads' jobs are still queued (Redis keeps its data),
    and failing them just as the worker comes back would make the user
    upload again a file that was about to import. Only the candidates are
    asked about, so this is one Redis lookup per stale-looking upload.

    Returns (failed ids, files deleted).
    """
    candidates = select(StatementUpload.id).where(stale_upload_condition(now))
    if upload_id is not None:
        candidates = candidates.where(StatementUpload.id == upload_id)
    lost = [candidate for candidate in db.scalars(candidates) if not job_is_waiting(candidate)]
    if not lost:
        return [], 0
    # The age condition again, in the UPDATE itself, so a row that changed
    # since the SELECT above is re-checked under its lock.
    return fail_uploads_where(
        db,
        and_(StatementUpload.id.in_(lost), stale_upload_condition(now)),
        STALE_UPLOAD_MESSAGE,
    )


# --- Step 1: bytes -> a parsed statement (no database) --------------------


def parse_statement(
    file_type: FileType,
    content: bytes,
    *,
    column_mapping: ColumnMapping | None = None,
) -> ParsedStatement:
    """Read and parse a stored file of a known type, or raise `InvalidStatementError`.

    Touches no database and no request, which is what lets the worker call
    it and what lets a unit test call it with two lines of bytes.

    Choosing the parser (SPEC §6.2): every registered bank parser is asked
    how confident it is that the file is its bank's, and the most confident
    one wins. Only if NONE recognises the file does `column_mapping` come
    into play - a mapping is the fallback, so a real ICICI file is read by
    the ICICI parser even if a mapping was sent along with it.

    Every `ParseError` from the readers and parsers is translated into
    `InvalidStatementError` here, in one place: the parser layer knows
    nothing about HTTP, and its messages are already written to be safe to
    show to a user (no narrations, no amounts - CLAUDE.md rule 3).
    """
    try:
        data = read_statement_as(file_type, content)
    except ParseError as exc:
        # The request already refused empty files, unsupported types and
        # PDFs (`_supported_file_type`), so in the worker this is a file
        # whose signature is right but whose contents cannot be read: a
        # damaged .xls, or a "CSV" that is not text.
        raise InvalidStatementError(str(exc)) from None

    parser: StatementParser | None = _registry().detect(data)
    if parser is None:
        if column_mapping is None:
            raise InvalidStatementError(NO_PARSER_MESSAGE)
        if data.file_type is not FileType.CSV:
            # A mapping names columns of a CSV table. An unrecognised .xls
            # (some other bank's workbook) cannot be read with one yet.
            raise InvalidStatementError(MAPPING_NEEDS_CSV_MESSAGE)
        parser = GenericCsvParser(column_mapping)

    try:
        return parser.parse(data)
    except ParseError as exc:
        raise InvalidStatementError(str(exc)) from None


# --- Step 2: a parsed statement -> rows in the database -------------------


def store_transactions(
    db: Session,
    upload: StatementUpload,
    rows: Sequence[RawRow],
) -> ImportCounts:
    """Insert a statement's rows, OLDEST FIRST, under an existing upload row.

    `upload` must already have an id (the caller flushed or committed it),
    because `transactions.upload_id` points at it. `rows` must be in
    chronological order (`to_chronological`): a row's position in the list
    becomes its `statement_seq` (1, 2, 3, ...), which is what orders the
    transactions of one day (ADR 014).

    This is where idempotency actually happens (SPEC §6.3, CLAUDE.md
    rule 5):

    - every row is normalized and fingerprinted (`fingerprints_for` also
      assigns the occurrence indexes, so two genuinely identical payments
      on one day stay two rows);
    - the INSERT carries `ON CONFLICT (account_id, fingerprint) DO NOTHING`,
      so a row that is already stored is skipped by the DATABASE. No "does
      this row exist?" query per row, and the result is still correct if
      two imports run at the same time.
    - `RETURNING id` tells us how many rows actually went in; the rest were
      duplicates.

    Nothing is committed here. The caller commits, so the upload row and
    its transactions appear together or not at all.
    """
    normalized = [normalize_row(row) for row in rows]
    fingerprints = fingerprints_for(upload.account_id, normalized)

    values = [
        {
            "user_id": upload.user_id,
            "account_id": upload.account_id,
            "upload_id": upload.id,
            "txn_date": row.raw.txn_date,
            "value_date": row.raw.value_date,
            "amount_paise": row.raw.amount_paise,
            "direction": row.raw.direction,
            "balance_after_paise": row.raw.balance_after_paise,
            "raw_description": row.raw.raw_description,
            "statement_seq": seq,
            "normalized_description": row.normalized_description,
            # Read from the narration (app/parsers/narration.py, ADR 015).
            # None of the three is in the fingerprint.
            "channel": row.channel,
            "counterparty_vpa": row.counterparty_vpa,
            "merchant_key": row.merchant_key,
            "fingerprint": fingerprint,
            # category_source and is_self_transfer keep their column
            # defaults ("none", false): the categorizer (Step 6) and
            # self-transfer detection (Step 8) decide them. merchant_id is
            # also the categorizer's: it links to a merchants ROW, found by
            # looking merchant_key up in the dictionary.
        }
        # start=1: positions are counted from 1, like row numbers.
        for seq, (row, fingerprint) in enumerate(
            zip(normalized, fingerprints, strict=True), start=1
        )
    ]

    inserted = 0
    for start in range(0, len(values), _INSERT_CHUNK_SIZE):
        chunk = values[start : start + _INSERT_CHUNK_SIZE]
        # postgresql.insert (not the generic sqlalchemy.insert) is the
        # dialect-specific construct that knows about ON CONFLICT.
        stmt = (
            insert(Transaction)
            .values(chunk)
            .on_conflict_do_nothing(index_elements=["account_id", "fingerprint"])
            .returning(Transaction.id)
        )
        # len() of the returned ids: DO NOTHING means a skipped row returns
        # nothing, so this counts exactly the rows that were new.
        inserted += len(db.execute(stmt).all())

    return ImportCounts(
        rows_parsed=len(values),
        rows_inserted=inserted,
        rows_duplicate=len(values) - inserted,
    )


# --- Helpers --------------------------------------------------------------


def _claim(db: Session, upload_id: uuid.UUID) -> StatementUpload | None:
    """Transaction 1 of `process_upload`: mark the upload `processing`.

    Returns the upload, or None, changing nothing, when there is nothing to
    do: the upload was deleted, or it is already final (`completed`,
    `failed`), which is what makes running the job again a no-op.

    `processing` is accepted as well as `pending`: it means an earlier
    attempt crashed half-way (its transaction 2 rolled back), and this is
    RQ's retry.
    """
    upload = db.get(StatementUpload, upload_id, with_for_update=True)
    if upload is None or upload.status not in UNFINISHED_STATUSES:
        db.rollback()
        log.info("statement_import_skipped", upload_id=str(upload_id))
        return None

    upload.status = UploadStatus.PROCESSING
    db.commit()
    return upload


def _lock_owners(db: Session, user_id: uuid.UUID, account_id: uuid.UUID) -> None:
    """Take `FOR KEY SHARE` on the upload's user and account rows.

    KEY SHARE is the weakest row lock: it only stops the row from being
    deleted (or its key changed), and it is exactly what the foreign-key
    checks of the INSERT into `transactions` take anyway. Taking it up
    front, before the upload row, fixes the lock order (see
    `process_upload`). A row that is already gone simply matches nothing.
    """
    db.execute(select(User.id).where(User.id == user_id).with_for_update(key_share=True))
    db.execute(select(Account.id).where(Account.id == account_id).with_for_update(key_share=True))


def _supported_file_type(filename: str, content: bytes) -> FileType:
    """The file's type from its signature, or `InvalidStatementError` (400).

    Only the first bytes are looked at (and the name, for a CSV, which has
    no signature); nothing is parsed. That is enough to refuse an empty
    file, a Word document or an `.xlsx` while the user is still waiting
    for the response.
    """
    try:
        file_type = detect_file_type(filename, content)
    except ParseError as exc:
        raise InvalidStatementError(str(exc)) from None
    # The readers' own list of what they can read, so this check and the
    # worker cannot disagree (only PDF is detected but unreadable today).
    if file_type not in READABLE_FILE_TYPES:
        raise InvalidStatementError(PDF_NOT_SUPPORTED_MESSAGE)
    return file_type


def _issues_to_json(issues: list[Issue] | None) -> list[dict[str, object]] | None:
    """Reconciliation issues for the JSONB column; None stays None ("not checked").

    Capped at MAX_STORED_ISSUES; `reconciliation_errors` keeps the full count.
    """
    if issues is None:
        return None
    return [
        {"row_number": issue.row_number, "kind": issue.kind.value}
        for issue in issues[:MAX_STORED_ISSUES]
    ]


def _mapping_to_json(mapping: ColumnMapping | None) -> dict[str, str | None] | None:
    """A `ColumnMapping` as a plain dict, for the JSONB column."""
    if mapping is None:
        return None
    # asdict() turns a dataclass into {"date_column": "Date", ...}. Every
    # field is a str or None, so the result is valid JSON as it is.
    return dataclasses.asdict(mapping)


def _mapping_from_json(stored: dict[str, str | None] | None) -> ColumnMapping | None:
    """The stored mapping back as a `ColumnMapping` (the reverse of the above).

    Building the dataclass runs its validation again (`__post_init__`). A
    stored mapping was validated on the way in, so that cannot fail unless
    the row was edited by hand - and then raising is right.
    """
    if stored is None:
        return None
    return ColumnMapping(**stored)


def _live_upload(db: Session, user_id: uuid.UUID, file_sha256: str) -> StatementUpload | None:
    """This user's live (not failed) upload of this exact file, if any (dedupe level 1).

    Scoped by user_id like every other query (rule 2): two users uploading
    the same file are two separate imports, and one user must not be able
    to learn what another has uploaded.

    A `failed` upload does not count: it is exactly the case where the user
    should be able to try the same file again (ADR 011).

    The "not failed" condition is the index's own WHERE clause, the same
    literal SQL (`NOT_FAILED_SQL`), not `status != UploadStatus.FAILED`.
    That way this check and the database cannot disagree on what a
    duplicate is, and Postgres can see that the query matches the partial
    index even in a prepared plan, where a bound parameter's value is
    unknown (see the constant in app/models/statement_upload.py).
    """
    stmt = (
        select(StatementUpload)
        .where(
            StatementUpload.user_id == user_id,
            StatementUpload.file_sha256 == file_sha256,
            text(NOT_FAILED_SQL),
        )
        # Re-read the row's columns even if this session already holds the
        # object: `create_upload` asks twice, and the second answer must
        # reflect what was committed in between, not a copy from the first.
        .execution_options(populate_existing=True)
    )
    return db.scalar(stmt)


def _raise_duplicate(existing: StatementUpload) -> NoReturn:
    """The 409 for a file this user already has a live upload of.

    The message depends on whether that upload has finished, and the id is
    in `details` either way, so the client can go and look at it.
    """
    details = {"upload_id": str(existing.id)}
    if existing.status in UNFINISHED_STATUSES:
        raise ConflictError(FILE_BEING_IMPORTED_MESSAGE, details=details)
    raise ConflictError(DUPLICATE_FILE_MESSAGE, details=details)


def _check_statement_matches_account(account: Account, statement: ParsedStatement) -> None:
    """Refuse a statement that clearly belongs to a different account.

    Picking the wrong account in a dropdown is an easy mistake, and its
    consequence is bad: another account's transactions silently merged into
    this one, with wrong balances and wrong insights, and no obvious way to
    tell afterwards. The statement itself usually knows better than the
    dropdown did.

    Only the LAST 4 DIGITS are compared, because four digits is all we are
    allowed to have (SPEC §7.3) - the parser truncates before this point,
    and `masked_number` is the same four digits. A statement that prints no
    account number (a bare CSV) cannot be checked, so it is accepted.

    The bank is deliberately NOT compared with `account.bank_code`. An
    account added as `GENERIC` may well be the ICICI account the file came
    from (that is what `GENERIC` is for before a bank has its own parser),
    so refusing on the bank would reject correct uploads, while the four
    digits catch the mistake that actually matters.

    The message never repeats the digits: it tells the user what to fix,
    not what the file contained.
    """
    if statement.account_last4 is not None and statement.account_last4 != account.masked_number:
        raise InvalidStatementError(WRONG_ACCOUNT_NUMBER_MESSAGE)


def _statement_period(statement: ParsedStatement) -> tuple[date | None, date | None]:
    """The period to record: the statement's own, else its rows' date range.

    ICICI's `.xls` prints the period in its header, a bare CSV does not,
    and a statement with no transactions has neither - which is why both
    columns are nullable.
    """
    if statement.period_start is not None and statement.period_end is not None:
        return statement.period_start, statement.period_end
    row_range = statement.row_date_range
    if row_range is not None:
        return row_range
    return None, None


def _safe_filename(filename: str) -> str:
    """Make a user-supplied filename safe to store and show.

    The name comes from the client, so it is untrusted text:

    - a browser may send a full path (`C:\\Users\\me\\statement.xls`), and
      only the last part is a name;
    - control characters have no place in a label, and a NUL byte cannot be
      stored in a Postgres text column at all (the driver raises, which
      would surface as a 500);
    - it is stored in a `VARCHAR(255)`, so a 10 MB "filename" must be cut
      here rather than rejected by the database.

    The file is never written to disk, so this is about storing a label, not
    about path traversal - but taking the basename removes that whole
    question as well.
    """
    name = PurePosixPath(filename.replace("\\", "/")).name
    name = "".join(char for char in name if char >= " " and char != "\x7f").strip()
    return name[:MAX_FILENAME_LENGTH] or "statement"
