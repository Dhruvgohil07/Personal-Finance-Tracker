"""Turn an uploaded statement file into transactions (SPEC §6.1, Step 7).

This is the pipeline the whole project exists for. It is written as three
functions, deliberately, so that the Phase 2 RQ worker can reuse the middle
of it without the HTTP request around it:

    import_statement()        the whole thing, called by POST /uploads
      |-- parse_statement()   bytes -> ParsedStatement   (no database at all)
      \\-- store_transactions()  ParsedStatement -> rows  (one transaction)

Order of checks in `import_statement`, and why that order:

1. **size**, before anything else - a 2 GB file must be refused without
   being parsed (SPEC §6.1).
2. **account ownership**, via `accounts_service.get_account`, which answers
   404 for an account that does not exist *or* belongs to someone else
   (rule 2, IDOR).
3. **sha256 of the bytes** -> was this exact file already uploaded by this
   user? That is dedupe level 1 (SPEC §6.3) and it is the cheapest check
   available, so it comes before parsing.
4. **read and parse**, which is where a file that is not a statement at all
   is found out.
5. **does this statement belong to the chosen account?** - the bank and,
   when the statement prints one, the last 4 digits of the account number.
6. **normalize -> fingerprint -> insert**, all in ONE database transaction.

What happens when an import fails: the request answers 4xx and **no row is
written**. `statement_uploads` therefore only ever holds successful imports
in Phase 1. That is on purpose and worth being able to explain:

- The import runs inside the request here, so the caller learns the reason
  from the response; nothing has to poll for it.
- `unique(user_id, file_sha256)` means a recorded failure would block the
  user from ever uploading that file again - so after we fixed a parser bug
  or added PDF support, their file would still be refused.

Phase 2 moves parsing into a worker, where the client cannot be told
synchronously any more; the row is then created first, and the `pending` /
`processing` / `failed` statuses start being used (see
`app/models/statement_upload.py`).
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import PurePosixPath

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError, InvalidStatementError, PayloadTooLargeError
from app.db.errors import violates_constraint
from app.models import Account, StatementUpload, Transaction, UploadStatus
from app.models.statement_upload import MAX_FILENAME_LENGTH
from app.parsers.base import FileType, ParsedStatement, ParseError, StatementParser
from app.parsers.fingerprint import fingerprints_for
from app.parsers.generic_csv import ColumnMapping, GenericCsvParser
from app.parsers.normalize import normalize_row
from app.parsers.readers import read_statement
from app.parsers.registry import ParserRegistry, build_default_registry
from app.services import accounts as accounts_service

log = structlog.get_logger()

DUPLICATE_FILE_MESSAGE = "You have already uploaded this file."
NO_PARSER_MESSAGE = (
    "We could not recognise this statement's layout. If it is a CSV, send a "
    "column_mapping describing which column holds the date, the description "
    "and the amount."
)
MAPPING_NEEDS_CSV_MESSAGE = "A column mapping can only be used with a CSV file."
WRONG_ACCOUNT_NUMBER_MESSAGE = (
    "This statement is for a different account number than the account you selected."
)

# How many transaction rows go into one INSERT statement.
#
# Not a performance tweak - a correctness limit. Postgres accepts at most
# 65535 bind parameters per statement, and each row here binds about 20
# (one per column), so a single INSERT caps out at roughly 3200 rows. A
# two-year statement would exceed that and fail with an error that says
# nothing about the real cause. All the chunks still run inside the SAME
# transaction, so the import stays all-or-nothing.
#
# TODO(dhruv): add an integration test that crosses this boundary - import a
# statement with more than 500 rows (build one with
# `tests/fixtures/icici_xls.py`, generating the rows in a loop) and assert
# that rows_inserted equals the number of rows and that a re-import counts
# all of them as duplicates. Without it, the chunking is the one branch in
# this module no test walks through.
_INSERT_CHUNK_SIZE = 500

# Name given by our naming convention (app/db/base.py), see migration 0005.
UPLOADS_FILE_HASH_CONSTRAINT = "uq_statement_uploads_user_id_file_sha256"


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


# --- Step 1: bytes -> a parsed statement (no database) --------------------


def parse_statement(
    filename: str,
    content: bytes,
    *,
    column_mapping: ColumnMapping | None = None,
) -> tuple[FileType, ParsedStatement]:
    """Read and parse an uploaded file, or raise `InvalidStatementError`.

    Touches no database and no request, which is what lets the Phase 2
    worker call it and what lets a unit test call it with two lines of
    bytes.

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
        data = read_statement(filename, content)
    except ParseError as exc:
        # Raised for an empty file, an unsupported type (including PDF,
        # which is Phase 2) and a file whose bytes we cannot read at all.
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
        statement = parser.parse(data)
    except ParseError as exc:
        raise InvalidStatementError(str(exc)) from None

    return data.file_type, statement


# --- Step 2: a parsed statement -> rows in the database -------------------


def store_transactions(
    db: Session,
    upload: StatementUpload,
    statement: ParsedStatement,
) -> ImportCounts:
    """Insert a parsed statement's rows under an existing upload row.

    `upload` must already have an id (the caller flushed or committed it),
    because `transactions.upload_id` points at it.

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
    normalized = [normalize_row(row) for row in statement.rows]
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
            "normalized_description": row.normalized_description,
            "fingerprint": fingerprint,
            # channel, category_source and is_self_transfer keep their
            # column defaults ("other", "none", false): detecting them is
            # Phase 2 / Step 8 work. merchant_key is computed by
            # normalize_row but has nowhere to go yet - transactions link to
            # a merchant ROW, and looking one up is the categorizer's job
            # (SPEC §8), not the importer's.
        }
        for row, fingerprint in zip(normalized, fingerprints, strict=True)
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


# --- The whole thing ------------------------------------------------------


def import_statement(
    db: Session,
    user_id: uuid.UUID,
    *,
    account_id: uuid.UUID,
    filename: str,
    content: bytes,
    column_mapping: ColumnMapping | None = None,
) -> StatementUpload:
    """Import a statement file end to end; return the completed upload row.

    Raises (all of which become one of our standard error responses):

    - `PayloadTooLargeError` (413) - over `MAX_UPLOAD_MB`
    - `NotFoundError` (404)        - unknown account, or someone else's
    - `ConflictError` (409)        - this exact file was already uploaded
    - `InvalidStatementError` (400) - unreadable file, unrecognised layout,
      or a statement that belongs to a different account
    """
    settings = get_settings()
    if len(content) > settings.max_upload_bytes:
        # Also enforced while reading the request (app/api/v1/uploads.py), so
        # the bytes never all arrive. This is the backstop for other callers.
        raise PayloadTooLargeError(
            f"The file is larger than the {settings.max_upload_mb} MB upload limit."
        )

    account = accounts_service.get_account(db, user_id, account_id)

    file_sha256 = hashlib.sha256(content).hexdigest()
    if _already_uploaded(db, user_id, file_sha256):
        raise ConflictError(DUPLICATE_FILE_MESSAGE)

    file_type, statement = parse_statement(filename, content, column_mapping=column_mapping)
    _check_statement_matches_account(account, statement)

    period_start, period_end = _statement_period(statement)

    upload = StatementUpload(
        user_id=user_id,
        account_id=account.id,
        original_filename=_safe_filename(filename),
        file_type=file_type,
        file_sha256=file_sha256,
        status=UploadStatus.COMPLETED,
        period_start=period_start,
        period_end=period_end,
    )
    db.add(upload)
    try:
        # flush() sends the INSERT but does NOT commit, so `upload.id` is
        # known and the transactions can point at it while everything stays
        # in one transaction.
        db.flush()
        counts = store_transactions(db, upload, statement)
        upload.rows_parsed = counts.rows_parsed
        upload.rows_inserted = counts.rows_inserted
        upload.rows_duplicate = counts.rows_duplicate
        upload.completed_at = datetime.now(UTC)
        db.commit()
        # IDs, the file type and counts only: no filename, no narration, no
        # amounts, no balances (CLAUDE.md rule 3).
        log.info(
            "statement_imported",
            user_id=str(user_id),
            account_id=str(account.id),
            upload_id=str(upload.id),
            file_type=file_type.value,
            rows_parsed=counts.rows_parsed,
            rows_inserted=counts.rows_inserted,
            rows_duplicate=counts.rows_duplicate,
        )
    except IntegrityError as exc:
        db.rollback()
        # The check above already answers 409 for a file we have seen. This
        # is the race: two uploads of the same file at the same time, where
        # both passed the check and the database rejected the second.
        if violates_constraint(exc, UPLOADS_FILE_HASH_CONSTRAINT):
            raise ConflictError(DUPLICATE_FILE_MESSAGE) from None
        raise

    return upload


# --- Helpers --------------------------------------------------------------


def _already_uploaded(db: Session, user_id: uuid.UUID, file_sha256: str) -> bool:
    """Has this user uploaded this exact file before? (dedupe level 1)

    Scoped by user_id like every other query (rule 2): two users uploading
    the same file are two separate imports, and one user must not be able
    to learn what another has uploaded.
    """
    stmt = select(StatementUpload.id).where(
        StatementUpload.user_id == user_id,
        StatementUpload.file_sha256 == file_sha256,
    )
    return db.scalar(stmt) is not None


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
