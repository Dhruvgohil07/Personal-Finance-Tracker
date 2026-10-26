"""Migration 0006 against real Postgres: `upload_blobs` and the partial index.

Two things are checked here, at the database level:

- `upload_blobs` (ADR 012): an encrypted file survives the trip through a
  `bytea` column byte for byte, an upload has at most one blob, the nonce
  must be 12 bytes, and deleting the upload deletes its blob.
- the file-hash index (ADR 011): two live uploads of the same file are
  still refused, but `failed` rows no longer count - which is the whole
  point of the change.

The rows are built directly with the ORM rather than through the API,
because what is being tested is the schema, not the endpoints. The
endpoint-level version of the second point is in test_uploads.py.
"""

import re
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.crypto import EncryptedFile, decrypt_file, encrypt_file
from app.db.errors import violates_constraint
from app.models import (
    Account,
    AccountType,
    BankCode,
    StatementUpload,
    UploadBlob,
    UploadStatus,
    User,
)
from app.models.statement_upload import FILE_HASH_INDEX, NOT_FAILED_SQL, USER_UPLOADS_INDEX
from app.parsers.base import FileType
from app.services import imports as imports_service

pytestmark = pytest.mark.integration

FILE_SHA256 = "ab" * 32
CONTENT = b"Transaction Date,Remarks,Withdrawal\r\n01/08/2026,UPI/SHOP/PAY,250.00\r\n"

UploadFactory = Callable[..., StatementUpload]


@pytest.fixture
def account(db: Session) -> Account:
    user = User(email="alice@example.com", password_hash="not-a-real-hash", name="Alice")
    db.add(user)
    db.flush()
    account = Account(
        user_id=user.id,
        bank_code=BankCode.ICICI,
        nickname="Salary account",
        account_type=AccountType.SAVINGS,
        masked_number="1234",
    )
    db.add(account)
    db.commit()
    return account


@pytest.fixture
def make_upload(db: Session, account: Account) -> UploadFactory:
    """Insert an upload row for `account`; commit it and return it."""

    def _make(
        status: UploadStatus = UploadStatus.PENDING, file_sha256: str = FILE_SHA256
    ) -> StatementUpload:
        upload = StatementUpload(
            user_id=account.user_id,
            account_id=account.id,
            original_filename="statement.xls",
            file_type=FileType.XLS,
            file_sha256=file_sha256,
            status=status,
        )
        db.add(upload)
        db.commit()
        return upload

    return _make


def store_blob(db: Session, upload: StatementUpload, content: bytes = CONTENT) -> None:
    encrypted = encrypt_file(content, upload.id)
    db.add(
        UploadBlob(
            upload_id=upload.id,
            encrypted_content=encrypted.ciphertext,
            nonce=encrypted.nonce,
        )
    )
    db.commit()


def count_blobs(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(UploadBlob)) or 0


# --- upload_blobs ---------------------------------------------------------


def test_an_encrypted_file_survives_the_database(db: Session, make_upload: UploadFactory) -> None:
    """Encrypt -> bytea -> read back -> decrypt gives the original bytes.

    Read back through a fresh query (`expire_all` drops the cached object),
    so the bytes really come from Postgres and not from Python's memory.
    """
    upload = make_upload()
    store_blob(db, upload)
    db.expire_all()

    blob = db.get(UploadBlob, upload.id)
    assert blob is not None
    assert blob.created_at is not None
    restored = decrypt_file(EncryptedFile(blob.nonce, blob.encrypted_content), upload.id)
    assert restored == CONTENT


def test_an_upload_has_at_most_one_blob(db: Session, make_upload: UploadFactory) -> None:
    upload = make_upload()
    store_blob(db, upload)

    with pytest.raises(IntegrityError):
        store_blob(db, upload, content=b"a second file")
    db.rollback()

    assert count_blobs(db) == 1


@pytest.mark.parametrize("length", [0, 11, 13, 16])
def test_a_nonce_of_the_wrong_length_is_refused(
    db: Session, make_upload: UploadFactory, length: int
) -> None:
    upload = make_upload()
    db.add(UploadBlob(upload_id=upload.id, encrypted_content=b"x", nonce=bytes(length)))

    with pytest.raises(IntegrityError) as excinfo:
        db.commit()
    db.rollback()

    assert violates_constraint(excinfo.value, "ck_upload_blobs_nonce_length")


def test_a_blob_needs_an_existing_upload(db: Session) -> None:
    db.add(UploadBlob(upload_id=uuid.uuid4(), encrypted_content=b"x", nonce=bytes(12)))

    with pytest.raises(IntegrityError) as excinfo:
        db.commit()
    db.rollback()

    assert violates_constraint(excinfo.value, "fk_upload_blobs_upload_id_statement_uploads")


def test_deleting_the_upload_deletes_its_blob(db: Session, make_upload: UploadFactory) -> None:
    """ON DELETE CASCADE: no encrypted file outlives the upload it belongs to."""
    upload = make_upload()
    store_blob(db, upload)

    db.delete(upload)
    db.commit()

    assert count_blobs(db) == 0


# --- The partial file-hash index ------------------------------------------


@pytest.mark.parametrize(
    "first_status",
    [
        UploadStatus.PENDING,
        UploadStatus.PROCESSING,
        UploadStatus.NEEDS_PASSWORD,
        UploadStatus.NEEDS_REVIEW,
        UploadStatus.COMPLETED,
    ],
)
def test_a_live_upload_still_blocks_the_same_file(
    db: Session, make_upload: UploadFactory, first_status: UploadStatus
) -> None:
    """Every status except `failed` still counts as "already uploaded".

    And the error names the index, which is how the import service tells a
    duplicate file (409) apart from any other IntegrityError (500).
    """
    make_upload(status=first_status)

    with pytest.raises(IntegrityError) as excinfo:
        make_upload(status=UploadStatus.PENDING)
    db.rollback()

    assert violates_constraint(excinfo.value, FILE_HASH_INDEX)


def test_a_failed_upload_does_not_block_the_same_file(
    db: Session, make_upload: UploadFactory
) -> None:
    make_upload(status=UploadStatus.FAILED)
    make_upload(status=UploadStatus.FAILED)  # a second failed attempt
    make_upload(status=UploadStatus.PENDING)  # and then a retry

    assert db.scalar(select(func.count()).select_from(StatementUpload)) == 3


def test_a_retry_cannot_be_made_live_twice(db: Session, make_upload: UploadFactory) -> None:
    """After a failure the file may come back - once.

    The partial index still holds among the non-failed rows: a failure
    frees the file for ONE new live upload, not for any number of them.
    """
    make_upload(status=UploadStatus.FAILED)
    make_upload(status=UploadStatus.PENDING)

    with pytest.raises(IntegrityError):
        make_upload(status=UploadStatus.PENDING)
    db.rollback()


def test_marking_an_upload_failed_frees_its_file(db: Session, make_upload: UploadFactory) -> None:
    """The index follows status CHANGES too, not just inserts.

    In Phase 2 the worker creates the row as `pending` and only later sets
    it to `failed`. At that moment the file must become uploadable again.
    """
    first = make_upload(status=UploadStatus.PENDING)
    first.status = UploadStatus.FAILED
    db.commit()

    make_upload(status=UploadStatus.PENDING)  # no IntegrityError


def test_the_same_file_from_two_users_is_two_uploads(
    db: Session, make_upload: UploadFactory
) -> None:
    """The index is per user, as the constraint was."""
    make_upload()
    bob = User(email="bob@example.com", password_hash="not-a-real-hash", name="Bob")
    db.add(bob)
    db.flush()
    bobs_account = Account(
        user_id=bob.id,
        bank_code=BankCode.ICICI,
        nickname="Salary account",
        account_type=AccountType.SAVINGS,
        masked_number="1234",
    )
    db.add(bobs_account)
    db.flush()
    db.add(
        StatementUpload(
            user_id=bob.id,
            account_id=bobs_account.id,
            original_filename="statement.xls",
            file_type=FileType.XLS,
            file_sha256=FILE_SHA256,
            status=UploadStatus.PENDING,
        )
    )
    db.commit()  # no IntegrityError


# --- Index use under prepared ("generic") plans ---------------------------
#
# psycopg prepares a query once it has run 5 times on a connection, and
# Postgres may then plan it once for ALL parameter values (a generic plan),
# without knowing what they are. A partial index can only be used when the
# query's condition provably implies the index's WHERE clause, so a value
# that arrives as a parameter is invisible to that proof. These tests force
# a generic plan and switch sequential scans off, which shows which indexes
# the query CAN use (the table is far too small for the planner to bother
# otherwise).


def _sql_with_numbered_params(statement: str, parameters: dict[str, Any]) -> tuple[str, list[str]]:
    """psycopg's `%(name)s` placeholders -> `$1, $2, ...` for PREPARE."""
    names: list[str] = []

    def number(match: re.Match[str]) -> str:
        names.append(match.group(1))
        return f"${len(names)}"

    sql = re.sub(r"%\((\w+)\)s", number, statement)
    return sql, [str(parameters[name]) for name in names]


def duplicate_check_sql(db: Session, user_id: uuid.UUID) -> tuple[str, dict[str, Any]]:
    """Run the service's duplicate check; return the SQL it sent, and its params.

    An event listener sees each statement on its way to the driver, so this
    is exactly what the import service executes, not a hand-written copy.
    """
    captured: list[tuple[str, dict[str, Any]]] = []

    def capture(_conn: Any, _cursor: Any, statement: str, parameters: Any, *_args: Any) -> None:
        captured.append((statement, parameters))

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        imports_service._live_upload(db, user_id, FILE_SHA256)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    [single] = captured
    return single


@pytest.fixture
def generic_plans(db: Session) -> Iterator[Callable[[str, list[str]], str]]:
    """EXPLAIN a statement as a prepared, generic plan; returns the plan text.

    PREPAREd statements belong to the CONNECTION, not the transaction, so
    each one is DEALLOCATEd again - otherwise the next test that gets this
    pooled connection would find the name already taken.
    """
    db.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
    db.execute(text("SET LOCAL enable_seqscan = off"))
    prepared: list[str] = []

    def explain(sql: str, args: list[str]) -> str:
        name = f"probe_{len(prepared)}"
        # The statement text is built from our own SQL (no user input), and
        # the arguments are passed as quoted literals of known-safe values.
        db.execute(text(f"PREPARE {name} AS {sql}"))
        prepared.append(name)
        literals = ", ".join(f"'{arg}'" for arg in args)
        plan = db.execute(text(f"EXPLAIN EXECUTE {name}({literals})")).scalars().all()
        return "\n".join(plan)

    yield explain
    for name in prepared:
        db.execute(text(f"DEALLOCATE {name}"))


def test_the_duplicate_check_sends_the_index_condition_as_literal_sql(
    db: Session, account: Account
) -> None:
    """`_live_upload` must not send 'failed' as a bound parameter."""
    statement, parameters = duplicate_check_sql(db, account.user_id)
    assert NOT_FAILED_SQL in statement
    assert "failed" not in [str(value) for value in parameters.values()]


def test_the_duplicate_check_uses_the_partial_index_in_a_generic_plan(
    db: Session, account: Account, generic_plans: Callable[[str, list[str]], str]
) -> None:
    """The real service query, prepared: an Index Scan on the partial index."""
    plan = generic_plans(*_sql_with_numbered_params(*duplicate_check_sql(db, account.user_id)))

    assert f"Index Scan using {FILE_HASH_INDEX}" in plan
    assert "Seq Scan" not in plan


def test_a_bound_status_would_not_use_the_partial_index(
    db: Session, account: Account, generic_plans: Callable[[str, list[str]], str]
) -> None:
    """The control experiment: why the condition must be literal SQL.

    The same query with 'failed' as a parameter cannot use the partial
    index, even with sequential scans switched off. Since migration 0007
    it falls back to the user index and FILTERS the hash and status row by
    row (before 0007 it was a full sequential scan of the table). If this
    ever fails, Postgres got smarter and the comment on `NOT_FAILED_SQL`
    in app/models/statement_upload.py can be revisited.
    """
    plan = generic_plans(
        "SELECT id FROM statement_uploads WHERE user_id = $1 AND file_sha256 = $2 AND status <> $3",
        [str(account.user_id), FILE_SHA256, "failed"],
    )

    assert FILE_HASH_INDEX not in plan
    assert "Filter" in plan


@pytest.mark.parametrize(
    "sql",
    [
        # The lookup behind ON DELETE CASCADE from users (DELETE /me).
        "SELECT id FROM statement_uploads WHERE user_id = $1",
        # The upload list (GET /uploads, Step 2): newest first.
        "SELECT id FROM statement_uploads WHERE user_id = $1 ORDER BY created_at DESC",
    ],
    ids=["cascade_lookup", "upload_list"],
)
def test_a_users_uploads_are_found_through_the_user_index(
    account: Account, generic_plans: Callable[[str, list[str]], str], sql: str
) -> None:
    """Every upload of a user, failed ones included, without a full scan.

    No Sort node for the list: the index already holds the rows in
    `created_at DESC` order.
    """
    plan = generic_plans(sql, [str(account.user_id)])

    assert USER_UPLOADS_INDEX in plan
    assert "Seq Scan" not in plan
    assert "Sort" not in plan
