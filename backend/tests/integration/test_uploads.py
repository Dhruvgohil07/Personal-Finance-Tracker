"""POST /uploads end to end: real app, real Postgres, real `.xls` bytes.

The statements here are built by `tests/fixtures/icici_xls.py`, which writes
a genuine (synthetic) ICICI workbook, so these tests cover the whole path an
upload really takes:

    multipart request -> reader -> parser -> normalize -> fingerprint
                      -> INSERT ... ON CONFLICT DO NOTHING

The tests that matter most, and the rules they defend:

- `test_reuploading_the_same_file_is_rejected`          dedupe level 1
- `test_an_overlapping_statement_inserts_only_new_rows` dedupe level 2
- `test_two_identical_rows_on_one_day_are_both_kept`    occurrence index
- `test_another_users_account_is_not_found`             rule 2 (IDOR)
- `test_the_log_has_no_narrations_or_amounts`            rule 3
"""

import datetime
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import PayloadTooLargeError
from app.models import StatementUpload, Transaction
from app.parsers.base import Direction
from app.services import imports as imports_service
from tests.fixtures.icici_xls import build_icici_xls

pytestmark = pytest.mark.integration

UPLOADS = "/api/v1/uploads"
ACCOUNTS = "/api/v1/accounts"

AuthHeadersFactory = Callable[[str], dict[str, str]]

# The synthetic statement's account number ends in these four digits.
STATEMENT_LAST4 = "1234"

# A statement covering 3-5 August that OVERLAPS the default fixture
# (which covers 1-4 August). The two shared rows are byte-for-byte the same
# transactions, but their "S No." differs - a real bank restarts the serial
# number in every statement, and it is deliberately not part of the
# fingerprint (ADR 006), so the overlap must still be recognised.
OVERLAPPING_ROWS = (
    ("1", "03/08/2026", "03/08/2026", "", "NEFT-SALARY-ACME", "0.00", "50000.00", "161431.33"),
    ("2", "04/08/2026", "04/08/2026", "", "MMT/IMPS/ACME BANK LTD", "1500.00", "0.00", "159931.33"),
    ("", "", "", "", "/205412345678/PAYMENT", "", "", ""),
    # The only genuinely new row.
    ("3", "05/08/2026", "05/08/2026", "", "UPI/NEWSHOP/PAY", "300.00", "0.00", "159631.33"),
)

# Two rows identical in EVERY field the fingerprint uses - same date, same
# amount, same narration, even the same balance. Only the occurrence index
# tells them apart (SPEC §6.3). Two ₹20 cups of chai, in other words.
IDENTICAL_ROWS = (
    ("1", "02/08/2026", "02/08/2026", "", "UPI/CHAI/PAY", "20.00", "0.00", "111451.33"),
    ("2", "02/08/2026", "02/08/2026", "", "UPI/CHAI/PAY", "20.00", "0.00", "111451.33"),
)

GENERIC_CSV = (
    b"Date,Narration,Withdrawal,Deposit,Balance\r\n"
    b"01/08/2026,SHOP PURCHASE,70.00,,111471.33\r\n"
    b"03/08/2026,SALARY CREDIT,,50000.00,161471.33\r\n"
)

CSV_MAPPING = (
    '{"date_column": "Date", "description_column": "Narration", '
    '"date_format": "%d/%m/%Y", "debit_column": "Withdrawal", '
    '"credit_column": "Deposit", "balance_column": "Balance"}'
)


# --- Fixtures and helpers ---------------------------------------------------


@pytest.fixture
def alice(make_auth_headers: AuthHeadersFactory) -> dict[str, str]:
    return make_auth_headers("alice@example.com")


@pytest.fixture
def bob(make_auth_headers: AuthHeadersFactory) -> dict[str, str]:
    return make_auth_headers("bob@example.com")


def make_account(
    client: TestClient, headers: dict[str, str], masked_number: str = STATEMENT_LAST4
) -> str:
    """Add an ICICI account through the real API and return its id."""
    response = client.post(
        ACCOUNTS,
        json={
            "bank_code": "ICICI",
            "nickname": "Salary account",
            "account_type": "savings",
            "masked_number": masked_number,
        },
        headers=headers,
    )
    assert response.status_code == 201
    account_id: str = response.json()["id"]
    return account_id


@pytest.fixture
def account_id(client: TestClient, alice: dict[str, str]) -> str:
    return make_account(client, alice)


def statement_bytes(tmp_path: Path, name: str = "statement.xls", **kwargs: Any) -> bytes:
    """A synthetic ICICI `.xls` as the bytes a browser would upload."""
    return build_icici_xls(tmp_path / name, **kwargs).read_bytes()


def post_upload(
    client: TestClient,
    headers: dict[str, str],
    *,
    content: bytes,
    account_id: str,
    filename: str = "statement.xls",
    column_mapping: str | None = None,
) -> Response:
    """POST /uploads as a multipart form, like the browser does."""
    data: dict[str, str] = {"account_id": account_id}
    if column_mapping is not None:
        data["column_mapping"] = column_mapping
    return client.post(
        UPLOADS,
        headers=headers,
        files={"file": (filename, content, "application/octet-stream")},
        data=data,
    )


def transactions_of(db: Session, upload_id: str) -> list[Transaction]:
    """One upload's transactions, oldest first (id breaks same-date ties)."""
    stmt = (
        select(Transaction)
        .where(Transaction.upload_id == uuid.UUID(upload_id))
        .order_by(Transaction.txn_date, Transaction.id)
    )
    return list(db.scalars(stmt))


def count_transactions(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Transaction)) or 0


# --- The happy path ---------------------------------------------------------


def test_upload_imports_the_whole_statement(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path
) -> None:
    """The default fixture has six body rows, one of which only continues a
    narration, so five transactions come out of it."""
    response = post_upload(client, alice, content=statement_bytes(tmp_path), account_id=account_id)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "completed"
    assert body["file_type"] == "xls"
    assert body["account_id"] == account_id
    assert body["original_filename"] == "statement.xls"
    assert (body["rows_parsed"], body["rows_inserted"], body["rows_duplicate"]) == (5, 5, 0)
    # rows_parsed = rows_inserted + rows_duplicate, always.
    assert body["rows_parsed"] == body["rows_inserted"] + body["rows_duplicate"]
    # The period comes from the statement's own header, not from the rows
    # (the rows stop on 4 August).
    assert body["period_start"] == "2026-08-01"
    assert body["period_end"] == "2026-08-31"
    assert body["completed_at"] is not None
    assert body["error_message"] is None
    # Reconciliation is Phase 2, so nothing is reported yet.
    assert body["reconciliation_errors"] == 0


def test_the_stored_rows_match_the_statement(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    """Every column, checked against the fixture by hand.

    The amounts are the real point: ₹70.00 must be exactly 7000 paise, with
    no float anywhere on the way (ADR 001).
    """
    response = post_upload(client, alice, content=statement_bytes(tmp_path), account_id=account_id)
    rows = transactions_of(db, response.json()["id"])

    assert [(row.txn_date, row.amount_paise, row.direction) for row in rows] == [
        (datetime.date(2026, 8, 1), 7000, Direction.DEBIT),
        (datetime.date(2026, 8, 2), 2000, Direction.DEBIT),
        (datetime.date(2026, 8, 2), 2000, Direction.DEBIT),
        (datetime.date(2026, 8, 3), 5000000, Direction.CREDIT),
        (datetime.date(2026, 8, 4), 150000, Direction.DEBIT),
    ]
    first, last = rows[0], rows[-1]
    assert first.balance_after_paise == 11147133
    assert first.raw_description == "UPI/TESTSHOP/PAY"
    assert first.normalized_description == "UPI/TESTSHOP/PAY"
    assert first.value_date == datetime.date(2026, 8, 1)
    # The narration the bank split over two rows is joined with nothing in
    # between, and the long reference number is stripped from the
    # normalized copy (SPEC §6.5) while the raw one keeps it.
    assert last.raw_description == "MMT/IMPS/ACME BANK LTD/205412345678/PAYMENT"
    assert last.normalized_description == "MMT/IMPS/ACME BANK LTD/ /PAYMENT"
    # Ownership and provenance are filled in on every row.
    assert {row.account_id for row in rows} == {uuid.UUID(account_id)}
    assert len({row.user_id for row in rows}) == 1
    # Not categorized yet: that is Step 8 (by hand) and Phase 4 (automatic).
    assert all(row.category_id is None for row in rows)
    assert all(row.category_source.value == "none" for row in rows)
    assert all(row.channel.value == "other" for row in rows)
    assert all(row.is_self_transfer is False for row in rows)
    # A fingerprint per row, all different (sha256 hex is 64 characters).
    fingerprints = {row.fingerprint for row in rows}
    assert len(fingerprints) == 5
    assert all(len(fingerprint) == 64 for fingerprint in fingerprints)
    # Same rule as the upload's repr: ids only, no amount and no narration.
    assert "7000" not in repr(first)
    assert "TESTSHOP" not in repr(first)


def test_the_upload_row_is_recorded(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    response = post_upload(client, alice, content=statement_bytes(tmp_path), account_id=account_id)

    upload = db.get(StatementUpload, uuid.UUID(response.json()["id"]))
    assert upload is not None
    assert upload.status.value == "completed"
    assert upload.rows_inserted == 5
    # sha256 of the bytes, lowercase hex - the dedupe key, never returned
    # to the client.
    assert len(upload.file_sha256) == 64
    assert "file_sha256" not in response.json()
    # A repr can end up in a log line or a traceback, so it carries ids
    # only - not the filename, which can identify a person (rule 3).
    assert "statement.xls" not in repr(upload)
    assert str(upload.id) in repr(upload)


def test_a_filename_with_a_path_is_stored_as_a_plain_name(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path
) -> None:
    """Browsers have been known to send a full path in the multipart header.

    The other half of the cleaning - control characters and over-long names
    - is unit tested in tests/unit/test_import_helpers.py, because an HTTP
    client percent-encodes those characters before they ever reach us.
    """
    response = post_upload(
        client,
        alice,
        content=statement_bytes(tmp_path),
        account_id=account_id,
        filename="C:\\Users\\me\\statement.xls",
    )

    assert response.status_code == 201
    assert response.json()["original_filename"] == "statement.xls"


# --- Idempotency (SPEC §6.3, CLAUDE.md rule 5) ------------------------------


def test_reuploading_the_same_file_is_rejected(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    """Dedupe level 1: the same bytes, rejected by unique(user_id, file_sha256)."""
    content = statement_bytes(tmp_path)
    assert post_upload(client, alice, content=content, account_id=account_id).status_code == 201

    again = post_upload(client, alice, content=content, account_id=account_id)

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "CONFLICT"
    # Nothing was added a second time, and no second upload row was written.
    assert count_transactions(db) == 5
    assert db.scalar(select(func.count()).select_from(StatementUpload)) == 1


def test_an_overlapping_statement_inserts_only_new_rows(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    """Dedupe level 2: 1-4 August, then 3-5 August.

    Three rows are parsed from the second file; two of them are already
    stored (3 and 4 August) and only the 5 August row is new.
    """
    post_upload(client, alice, content=statement_bytes(tmp_path), account_id=account_id)

    second = post_upload(
        client,
        alice,
        content=statement_bytes(
            tmp_path,
            name="second.xls",
            rows=OVERLAPPING_ROWS,
            period_from="03/08/2026",
            period_to="05/08/2026",
        ),
        account_id=account_id,
        filename="second.xls",
    )

    assert second.status_code == 201
    body = second.json()
    assert (body["rows_parsed"], body["rows_inserted"], body["rows_duplicate"]) == (3, 1, 2)
    assert count_transactions(db) == 6
    # The new row is attached to the SECOND upload, and the shared rows
    # still belong to the first one: a duplicate is skipped, not moved.
    assert len(transactions_of(db, body["id"])) == 1


def test_two_identical_rows_on_one_day_are_both_kept(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    """Two ₹20 chai payments on the same day are two real transactions.

    They are identical in every field the fingerprint uses, so only
    `occurrence_index` separates them. Losing one would lose real money
    from the user's totals.
    """
    response = post_upload(
        client,
        alice,
        content=statement_bytes(tmp_path, rows=IDENTICAL_ROWS),
        account_id=account_id,
    )

    assert response.json()["rows_inserted"] == 2
    assert count_transactions(db) == 2


def test_the_occurrence_index_is_stable_across_imports(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    """The same two identical rows, in a different file: still two rows.

    This is the other half of the occurrence index. The number is assigned
    per identity group in statement order, so both copies of the pair
    fingerprint the same way and the second import adds nothing.
    """
    post_upload(
        client,
        alice,
        content=statement_bytes(tmp_path, rows=IDENTICAL_ROWS),
        account_id=account_id,
    )

    # Same two transactions, but a file with a different period (so a
    # different sha256, which means dedupe level 1 does not fire).
    again = post_upload(
        client,
        alice,
        content=statement_bytes(
            tmp_path,
            name="again.xls",
            rows=IDENTICAL_ROWS,
            period_from="01/08/2026",
            period_to="05/08/2026",
        ),
        account_id=account_id,
        filename="again.xls",
    )

    assert (again.json()["rows_inserted"], again.json()["rows_duplicate"]) == (0, 2)
    assert count_transactions(db) == 2


# --- Ownership (CLAUDE.md rule 2) -------------------------------------------


def test_another_users_account_is_not_found(
    client: TestClient,
    alice: dict[str, str],
    bob: dict[str, str],
    account_id: str,
    tmp_path: Path,
    db: Session,
) -> None:
    """IDOR: Bob must not be able to import into Alice's account.

    404, not 403: a different answer would tell him the id exists.
    """
    response = post_upload(client, bob, content=statement_bytes(tmp_path), account_id=account_id)

    assert response.status_code == 404
    assert count_transactions(db) == 0


def test_an_unknown_account_is_not_found(
    client: TestClient, alice: dict[str, str], tmp_path: Path
) -> None:
    response = post_upload(
        client, alice, content=statement_bytes(tmp_path), account_id=str(uuid.uuid4())
    )

    assert response.status_code == 404


def test_two_users_can_upload_the_same_file(
    client: TestClient,
    alice: dict[str, str],
    bob: dict[str, str],
    account_id: str,
    tmp_path: Path,
    db: Session,
) -> None:
    """Dedupe level 1 is scoped per user: a shared household statement is
    two separate imports, and each user only ever sees their own rows."""
    content = statement_bytes(tmp_path)
    bobs_account = make_account(client, bob)

    assert post_upload(client, alice, content=content, account_id=account_id).status_code == 201
    assert post_upload(client, bob, content=content, account_id=bobs_account).status_code == 201

    assert count_transactions(db) == 10
    per_user = db.execute(
        select(Transaction.user_id, func.count()).group_by(Transaction.user_id)
    ).all()
    assert sorted(count for _user_id, count in per_user) == [5, 5]


def test_an_upload_requires_authentication(client: TestClient, tmp_path: Path) -> None:
    response = post_upload(
        client, {}, content=statement_bytes(tmp_path), account_id=str(uuid.uuid4())
    )

    assert response.status_code == 401


def test_deleting_the_account_deletes_its_transactions(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    """The ON DELETE CASCADE chain, proven rather than assumed. It is what
    makes `DELETE /me` a real hard delete (SPEC §7.3)."""
    post_upload(client, alice, content=statement_bytes(tmp_path), account_id=account_id)
    assert count_transactions(db) == 5

    assert client.delete(f"{ACCOUNTS}/{account_id}", headers=alice).status_code == 204

    assert count_transactions(db) == 0
    assert db.scalar(select(func.count()).select_from(StatementUpload)) == 0


# --- Files we refuse --------------------------------------------------------


def test_a_pdf_is_rejected(
    client: TestClient, alice: dict[str, str], account_id: str, db: Session
) -> None:
    """PDF parsing is Phase 2. The message says so, and - importantly - no
    upload row is written, so the same file can be imported once we support
    it (see app/services/imports.py)."""
    response = post_upload(
        client,
        alice,
        content=b"%PDF-1.7\n%fake\n",
        account_id=account_id,
        filename="statement.pdf",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_STATEMENT"
    assert "PDF" in response.json()["error"]["message"]
    assert db.scalar(select(func.count()).select_from(StatementUpload)) == 0


def test_an_empty_file_is_rejected(
    client: TestClient, alice: dict[str, str], account_id: str
) -> None:
    response = post_upload(client, alice, content=b"", account_id=account_id)

    assert response.status_code == 400
    assert "empty" in response.json()["error"]["message"]


def test_an_unsupported_file_type_is_rejected(
    client: TestClient, alice: dict[str, str], account_id: str
) -> None:
    response = post_upload(
        client, alice, content=b"just some notes", account_id=account_id, filename="notes.docx"
    )

    assert response.status_code == 400


def test_a_statement_for_a_different_account_number_is_rejected(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    """The statement's own last 4 digits beat the dropdown: picking the
    wrong account would merge another account's rows into this one."""
    other_account = make_account(client, alice, masked_number="9999")

    response = post_upload(
        client, alice, content=statement_bytes(tmp_path), account_id=other_account
    )

    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "different account number" in message
    # The message must not repeat the digits it read out of the file.
    assert STATEMENT_LAST4 not in message
    assert count_transactions(db) == 0


def test_a_file_over_the_size_limit_is_rejected(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The limit is lowered to 1 MB for the test instead of sending 10 MB.

    `get_settings()` is cached, so the route, the service and this test all
    look at the same Settings object.
    """
    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)

    response = post_upload(client, alice, content=b"x" * (2 * 1024 * 1024), account_id=account_id)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_the_service_enforces_the_size_limit_too(
    db: Session, account_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route stops reading at the limit, and the service checks again.

    Two checks for one rule, because they protect different callers: the
    route protects the API, and the service is what the Phase 2 worker will
    call with bytes that never came through a request.
    """
    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)

    with pytest.raises(PayloadTooLargeError):
        imports_service.import_statement(
            db,
            uuid.uuid4(),
            account_id=uuid.UUID(account_id),
            filename="statement.xls",
            content=b"x" * (2 * 1024 * 1024),
        )


def test_a_statement_that_cannot_be_parsed_is_rejected(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    """A file the ICICI parser recognises but cannot read: the error names
    the row, and still no upload row is written (ADR 008)."""
    broken = (("1", "01/08/2026", "01/08/2026", "", "UPI/SHOP/PAY", "seventy", "", "111471.33"),)

    response = post_upload(
        client, alice, content=statement_bytes(tmp_path, rows=broken), account_id=account_id
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_STATEMENT"
    # ParseError messages carry the row number and never the cell's value.
    assert "row " in response.json()["error"]["message"]
    assert db.scalar(select(func.count()).select_from(StatementUpload)) == 0


def test_a_simultaneous_duplicate_upload_is_a_conflict(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The race the unique constraint exists for.

    Two uploads of the same file at the same moment can both pass the
    "already uploaded?" query and only be stopped by the database. That is
    simulated here by making the query always answer "no", so the second
    upload reaches the INSERT - and must still be a clean 409, not a 500.
    """
    content = statement_bytes(tmp_path)
    assert post_upload(client, alice, content=content, account_id=account_id).status_code == 201

    monkeypatch.setattr(imports_service, "_already_uploaded", lambda *_args: False)
    second = post_upload(client, alice, content=content, account_id=account_id)

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CONFLICT"


# --- Generic CSV + column mapping -------------------------------------------


def test_a_generic_csv_is_imported_with_a_column_mapping(
    client: TestClient, alice: dict[str, str], account_id: str, db: Session
) -> None:
    response = post_upload(
        client,
        alice,
        content=GENERIC_CSV,
        account_id=account_id,
        filename="export.csv",
        column_mapping=CSV_MAPPING,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["file_type"] == "csv"
    assert (body["rows_parsed"], body["rows_inserted"]) == (2, 2)
    # A bare CSV states no period, so it is taken from the rows themselves.
    assert (body["period_start"], body["period_end"]) == ("2026-08-01", "2026-08-03")

    rows = transactions_of(db, body["id"])
    assert [(row.amount_paise, row.direction) for row in rows] == [
        (7000, Direction.DEBIT),
        (5000000, Direction.CREDIT),
    ]


def test_a_csv_without_a_mapping_is_rejected(
    client: TestClient, alice: dict[str, str], account_id: str
) -> None:
    """No bank parser recognises a generic CSV, and without a mapping there
    is no way to know which column is which. The message says what to send."""
    response = post_upload(
        client, alice, content=GENERIC_CSV, account_id=account_id, filename="export.csv"
    )

    assert response.status_code == 400
    assert "column_mapping" in response.json()["error"]["message"]


def test_an_invalid_column_mapping_is_a_validation_error(
    client: TestClient, alice: dict[str, str], account_id: str
) -> None:
    """A mapping arrives as a JSON string in a form field, so it is
    validated by hand - and must still produce our normal 422 shape, with
    the field path inside `column_mapping`."""
    response = post_upload(
        client,
        alice,
        content=GENERIC_CSV,
        account_id=account_id,
        filename="export.csv",
        column_mapping='{"date_column": "Date", "description_column": "Narration", '
        '"date_format": "%d/%m/%Y"}',
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert any(
        detail["field"].startswith("body.column_mapping") for detail in body["error"]["details"]
    )


def test_a_mapping_is_ignored_when_a_bank_parser_recognises_the_file(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path
) -> None:
    """A mapping is the FALLBACK (SPEC §6.2). A real ICICI file is read by
    the ICICI parser even if a mapping was sent too - the mapping's columns
    do not exist in that workbook, so if it were used the import would
    fail."""
    response = post_upload(
        client,
        alice,
        content=statement_bytes(tmp_path),
        account_id=account_id,
        column_mapping=CSV_MAPPING,
    )

    assert response.status_code == 201
    assert response.json()["rows_inserted"] == 5


def test_a_mapping_cannot_rescue_an_unrecognised_workbook(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path
) -> None:
    """A mapping names columns of a CSV table, so it cannot be applied to a
    `.xls` no parser recognises.

    The file is a real workbook (so the reader accepts it) with header
    labels no parser knows (so `detect()` finds nothing).
    """
    unknown_bank = statement_bytes(tmp_path, header_labels=("A", "B", "C", "D", "E", "F", "G", "H"))

    response = post_upload(
        client,
        alice,
        content=unknown_bank,
        account_id=account_id,
        column_mapping=CSV_MAPPING,
    )

    assert response.status_code == 400
    assert "CSV" in response.json()["error"]["message"]


def test_an_unrecognised_workbook_without_a_mapping_is_rejected(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path
) -> None:
    unknown_bank = statement_bytes(tmp_path, header_labels=("A", "B", "C", "D", "E", "F", "G", "H"))

    response = post_upload(client, alice, content=unknown_bank, account_id=account_id)

    assert response.status_code == 400
    assert "could not recognise" in response.json()["error"]["message"]


# --- Privacy (CLAUDE.md rule 3) ---------------------------------------------


def test_the_log_has_no_narrations_or_amounts(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path
) -> None:
    """An import logs IDs, the file type and counts - nothing else.

    `capture_logs` collects every structlog event raised during the
    request, so this covers the import service, the request logger and
    anything else on the way.
    """
    content = statement_bytes(tmp_path)

    with structlog.testing.capture_logs() as events:
        response = post_upload(client, alice, content=content, account_id=account_id)

    assert response.status_code == 201
    text = str(events)
    for secret in (
        "TESTSHOP",  # a narration
        "CHAISTALL",
        "SALARY",
        "111471",  # a balance, in rupees and in paise
        "11147133",
        "50000.00",  # an amount as printed
        "5000000",  # the same amount in paise
        "084600001234",  # the full account number
        "statement.xls",  # the filename
    ):
        assert secret not in text, f"{secret!r} must never be logged"

    imported = [event for event in events if event["event"] == "statement_imported"]
    assert len(imported) == 1
    assert imported[0]["rows_inserted"] == 5
