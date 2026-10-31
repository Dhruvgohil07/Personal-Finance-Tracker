"""POST /uploads end to end: real app, real Postgres, real `.xls` bytes.

The statements here are built by `tests/fixtures/icici_xls.py`, which writes
a genuine (synthetic) ICICI workbook, so these tests cover the whole path an
upload really takes:

    multipart request -> encrypted blob + `pending` row -> RQ job
        -> decrypt -> reader -> parser -> normalize -> fingerprint
        -> INSERT ... ON CONFLICT DO NOTHING -> `completed`

The job runs on the `sync_queue` (tests/integration/conftest.py), so by the
time POST answers 202 it has already finished, and `GET /uploads/{id}`
shows the final result. That is why most tests here are "POST, then GET".
The worker's own edge cases (retries, a missing file, running a job twice)
are in test_upload_worker.py, and GET/DELETE in test_upload_endpoints.py.

The tests that matter most, and the rules they defend:

- `test_reuploading_the_same_file_is_rejected`          dedupe level 1
- `test_an_overlapping_statement_inserts_only_new_rows` dedupe level 2
- `test_two_identical_rows_on_one_day_are_both_kept`    occurrence index
- `test_another_users_account_is_not_found`             rule 2 (IDOR)
- `test_the_log_has_no_narrations_or_amounts`            rule 3
"""

import datetime
import hashlib
import uuid
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient
from httpx import Response
from rq import Queue
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import PayloadTooLargeError
from app.models import Account, StatementUpload, Transaction, UploadBlob, UploadStatus
from app.parsers.base import Channel, Direction, FileType
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


def many_rows(count: int) -> tuple[tuple[str, ...], ...]:
    """`count` ICICI body rows: one debit each, dates ascending.

    Generated rather than written out as a literal, because the only
    interesting thing about these rows is how MANY there are.

    Three properties the generated rows must have, each for a reason:

    - **a different amount per row**, so every row is a genuinely distinct
      transaction with its own fingerprint. Identical rows would survive
      too (that is what the occurrence index is for), but then a failure
      could mean either bug, and this test is about chunking.
    - **ascending dates** (four rows per day), so the statement is read
      in file order and its balances chain: nothing but the chunking is
      going on.
    - **money as `Decimal`**, formatted to two decimal places. Project
      rule 1 holds in test fixtures too: a float here could produce a cell
      like "1.0000000000000002" and the import would rightly refuse it.
    """
    rows = []
    balance = Decimal("200000.00")

    for index in range(count):
        # 1.00, 1.01, 1.02, ... - distinct, and small enough that the
        # running balance stays positive for any realistic count.
        amount = Decimal("1.00") + Decimal(index) / 100
        balance -= amount
        txn_date = datetime.date(2026, 8, 1) + datetime.timedelta(days=index // 4)
        printed_date = txn_date.strftime("%d/%m/%Y")

        # serial, value date, txn date, cheque, remarks, withdrawal, deposit, balance
        rows.append(
            (
                str(index + 1),
                printed_date,
                printed_date,
                "",
                f"UPI/SHOP{index:04d}/PAY",
                f"{amount:.2f}",
                "0.00",
                f"{balance:.2f}",
            )
        )

    return tuple(rows)


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


def get_upload(client: TestClient, headers: dict[str, str], upload_id: str) -> dict[str, Any]:
    """GET /uploads/{id}: what a client polling for the result sees."""
    response = client.get(f"{UPLOADS}/{upload_id}", headers=headers)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def import_file(
    client: TestClient,
    headers: dict[str, str],
    *,
    content: bytes,
    account_id: str,
    filename: str = "statement.xls",
    column_mapping: str | None = None,
) -> dict[str, Any]:
    """Upload a file and return the upload as it is once the job has run.

    POST must accept it (202). The job has then already run on the
    synchronous test queue, so one GET gives the final state.
    """
    response = post_upload(
        client,
        headers,
        content=content,
        account_id=account_id,
        filename=filename,
        column_mapping=column_mapping,
    )
    assert response.status_code == 202
    return get_upload(client, headers, response.json()["id"])


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


def count_blobs(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(UploadBlob)) or 0


# --- The happy path ---------------------------------------------------------


def test_an_upload_is_accepted_and_then_imported(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path
) -> None:
    """202 with a `pending` upload, then the finished import on GET.

    The default fixture has six body rows, one of which only continues a
    narration, so five transactions come out of it.
    """
    response = post_upload(client, alice, content=statement_bytes(tmp_path), account_id=account_id)

    # Accepted, not done: the response describes the upload as it was when
    # the request handed it to the queue (ADR 013).
    assert response.status_code == 202
    accepted = response.json()
    assert accepted["status"] == "pending"
    assert (accepted["rows_parsed"], accepted["rows_inserted"]) == (0, 0)
    assert accepted["completed_at"] is None
    assert accepted["file_type"] == "xls"

    body = get_upload(client, alice, accepted["id"])
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
    assert all(row.is_self_transfer is False for row in rows)
    # Read from the narration (Phase 2 Step 5). These fixture narrations
    # are short, not ICICI's full layout, so the generic first-word rules
    # apply; `test_narration_facts_are_stored` covers the real layout.
    assert [(row.channel, row.merchant_key) for row in rows] == [
        (Channel.UPI, "TESTSHOP"),
        (Channel.UPI, "CHAISTALL"),
        (Channel.UPI, "CHAISTALL"),
        (Channel.NEFT, "SALARY"),
        (Channel.IMPS, "ACME"),
    ]
    # A fingerprint per row, all different (sha256 hex is 64 characters).
    fingerprints = {row.fingerprint for row in rows}
    assert len(fingerprints) == 5
    assert all(len(fingerprint) == 64 for fingerprint in fingerprints)
    # Same rule as the upload's repr: ids only, no amount and no narration.
    assert "7000" not in repr(first)
    assert "TESTSHOP" not in repr(first)


# Narrations in the layouts found in the real ICICI statement (ADR 015), with
# invented names, VPAs and reference numbers (CLAUDE.md rule 8).
ICICI_LAYOUT_ROWS = (
    (
        "1", "01/08/2026", "01/08/2026", "",
        "UPI/MOON CAFE/Q123456789@ybl/NO REMARK/YES BANK L/512345678901/"
        "ICI4f2a9c1e7b3d5f6a8c0e2b4d6f8a1c3e",
        "120.00", "0.00", "111421.33",
    ),
    (
        "2", "02/08/2026", "02/08/2026", "",
        "UPI/GREEN LEAF/gpay-123456789/UPI/AXIS BANK/512345678902/"
        "AXI4f2a9c1e7b3d5f6a8c0e2b4d6f8a1c3e/",
        "80.00", "0.00", "111341.33",
    ),
    (
        "3", "03/08/2026", "03/08/2026", "",
        "MMT/IMPS/612345678912/Refund of fee/ASHA TRADE/State Bank of I",
        "0.00", "1000.00", "112341.33",
    ),
    (
        "4", "04/08/2026", "04/08/2026", "",
        "NFS/CASH WDL/612345678913/12345678/MUMBAI CEN/04-08-261430",
        "2000.00", "0.00", "110341.33",
    ),
)  # fmt: skip


def test_narration_facts_are_stored(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    """Channel, VPA and merchant key reach the database and the API (Step 5)."""
    content = statement_bytes(tmp_path, rows=ICICI_LAYOUT_ROWS)

    body = import_file(client, alice, content=content, account_id=account_id)
    rows = transactions_of(db, body["id"])

    assert body["status"] == "completed"
    assert [(row.channel, row.counterparty_vpa, row.merchant_key) for row in rows] == [
        (Channel.UPI, "q123456789@ybl", "MOONCAFE"),
        # Cut at 14 characters by the bank, stored as printed
        (Channel.UPI, "gpay-123456789", "GREENLEAF"),
        (Channel.IMPS, None, "ASHATRADE"),
        # Cash: no counterparty
        (Channel.ATM, None, None),
    ]

    # The list endpoint shows channel and VPA (it already had the fields;
    # until now they were always "other" and null).
    listed = client.get("/api/v1/transactions", headers=alice).json()["items"]
    by_vpa = {item["counterparty_vpa"]: item["channel"] for item in listed}
    assert by_vpa["q123456789@ybl"] == "upi"


def test_the_upload_row_is_recorded(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    response = post_upload(client, alice, content=statement_bytes(tmp_path), account_id=account_id)

    upload = db.get(StatementUpload, uuid.UUID(response.json()["id"]))
    assert upload is not None
    assert upload.status.value == "completed"
    assert upload.rows_inserted == 5
    # The encrypted file is deleted once its rows are stored (SPEC §7.3),
    # and no column mapping was sent with this file.
    assert db.get(UploadBlob, upload.id) is None
    assert upload.column_mapping is None
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

    assert response.status_code == 202
    assert response.json()["original_filename"] == "statement.xls"


def test_a_csv_whose_name_is_cut_short_still_imports(
    client: TestClient, alice: dict[str, str], account_id: str
) -> None:
    """The stored filename is cut to 255 characters, which can cut off the
    ".csv" - and for a CSV the name is the only sign of what it is.

    The worker must therefore not judge the file by its STORED name. It
    uses the type the request already decided (`file_type`), so this file
    imports instead of failing as "unsupported file type".
    """
    upload = import_file(
        client,
        alice,
        content=GENERIC_CSV,
        account_id=account_id,
        filename="a" * 300 + ".csv",
        column_mapping=CSV_MAPPING,
    )

    assert len(upload["original_filename"]) == 255
    assert not upload["original_filename"].endswith(".csv")
    assert upload["status"] == "completed"
    assert upload["rows_inserted"] == 2


# --- Idempotency (SPEC §6.3, CLAUDE.md rule 5) ------------------------------


def test_reuploading_the_same_file_is_rejected(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    """Dedupe level 1: the same bytes, rejected by the file-hash index (ADR 011)."""
    content = statement_bytes(tmp_path)
    first = post_upload(client, alice, content=content, account_id=account_id)
    assert first.status_code == 202

    again = post_upload(client, alice, content=content, account_id=account_id)

    assert again.status_code == 409
    error = again.json()["error"]
    assert error["code"] == "CONFLICT"
    assert error["message"] == imports_service.DUPLICATE_FILE_MESSAGE
    # The existing upload is named, so the client can show it.
    assert error["details"] == {"upload_id": first.json()["id"]}
    # Nothing was added a second time, and no second upload row was written.
    assert count_transactions(db) == 5
    assert db.scalar(select(func.count()).select_from(StatementUpload)) == 1


def test_a_failed_upload_does_not_block_the_same_file(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    """ADR 011: a `failed` row is not "already uploaded".

    The failed row is inserted directly, standing in for an earlier attempt
    that hit a parser bug. The same file must then import normally -
    otherwise the user could never retry it after the fix.
    (`test_a_file_that_failed_can_be_uploaded_again` does the same through
    a real failed import.)
    """
    content = statement_bytes(tmp_path)
    account = db.scalars(select(Account).where(Account.id == uuid.UUID(account_id))).one()
    db.add(
        StatementUpload(
            user_id=account.user_id,
            account_id=account.id,
            original_filename="statement.xls",
            file_type=FileType.XLS,
            file_sha256=hashlib.sha256(content).hexdigest(),
            status=UploadStatus.FAILED,
            error_message="We could not read this statement.",
        )
    )
    db.commit()

    upload = import_file(client, alice, content=content, account_id=account_id)

    assert upload["rows_inserted"] == 5
    # The failed attempt is kept alongside the successful one.
    statuses = db.scalars(select(StatementUpload.status)).all()
    assert sorted(statuses) == [UploadStatus.COMPLETED, UploadStatus.FAILED]


def test_an_overlapping_statement_inserts_only_new_rows(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    """Dedupe level 2: 1-4 August, then 3-5 August.

    Three rows are parsed from the second file; two of them are already
    stored (3 and 4 August) and only the 5 August row is new.
    """
    post_upload(client, alice, content=statement_bytes(tmp_path), account_id=account_id)

    body = import_file(
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
    upload = import_file(
        client,
        alice,
        content=statement_bytes(tmp_path, rows=IDENTICAL_ROWS),
        account_id=account_id,
    )

    assert upload["rows_inserted"] == 2
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
    again = import_file(
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

    assert (again["rows_inserted"], again["rows_duplicate"]) == (0, 2)
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

    assert post_upload(client, alice, content=content, account_id=account_id).status_code == 202
    assert post_upload(client, bob, content=content, account_id=bobs_account).status_code == 202

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


def test_a_damaged_pdf_is_accepted_then_fails_with_a_message(
    client: TestClient, alice: dict[str, str], account_id: str
) -> None:
    """PDFs are imported since Phase 2 Step 9. The signature alone cannot
    tell a damaged PDF from a good one, so it is accepted (202) and the
    worker reports it on the upload, like any unreadable file."""
    body = import_file(
        client, alice, content=b"%PDF-1.7\n%fake\n", account_id=account_id, filename="s.pdf"
    )

    assert body["status"] == "failed"
    assert body["error_message"] == "this PDF could not be read"


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


def test_a_statement_for_a_different_account_number_fails(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    """The statement's own last 4 digits beat the dropdown: picking the
    wrong account would merge another account's rows into this one.

    The digits are inside the file, so only the worker can see them: the
    upload is accepted, then fails with the reason in `error_message`.
    """
    other_account = make_account(client, alice, masked_number="9999")

    upload = import_file(client, alice, content=statement_bytes(tmp_path), account_id=other_account)

    assert upload["status"] == "failed"
    message = upload["error_message"]
    assert message == imports_service.WRONG_ACCOUNT_NUMBER_MESSAGE
    # The message must not repeat the digits it read out of the file.
    assert STATEMENT_LAST4 not in message
    assert count_transactions(db) == 0
    # A failed import deletes its stored file too.
    assert count_blobs(db) == 0


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
    db: Session, account_id: str, sync_queue: Queue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route stops reading at the limit, and the service checks again.

    Two checks for one rule, because they protect different callers: the
    route protects the API, and the service is the backstop for any other
    code that hands it bytes.
    """
    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)

    with pytest.raises(PayloadTooLargeError):
        imports_service.create_upload(
            db,
            uuid.uuid4(),
            account_id=uuid.UUID(account_id),
            filename="statement.xls",
            content=b"x" * (2 * 1024 * 1024),
            queue=sync_queue,
        )


def test_a_statement_that_cannot_be_parsed_fails(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    """A file the ICICI parser recognises but cannot read.

    The upload is kept as `failed` (ADR 011), with the parser's message:
    it names the row and never the cell's value. Nothing is imported and
    the stored file is deleted.
    """
    broken = (("1", "01/08/2026", "01/08/2026", "", "UPI/SHOP/PAY", "seventy", "", "111471.33"),)

    upload = import_file(
        client, alice, content=statement_bytes(tmp_path, rows=broken), account_id=account_id
    )

    assert upload["status"] == "failed"
    assert "row " in upload["error_message"]
    assert "seventy" not in upload["error_message"]
    assert upload["completed_at"] is None
    assert count_transactions(db) == 0
    assert count_blobs(db) == 0


def test_a_damaged_workbook_fails(
    client: TestClient, alice: dict[str, str], account_id: str, db: Session
) -> None:
    """The first bytes say "Excel workbook", the rest is garbage.

    The request only looks at the signature, so it accepts the file; the
    worker is where it turns out to be unreadable. That must be an ordinary
    `failed` upload with the reader's message, not a crash and a retry.
    """
    ole2_signature = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

    upload = import_file(
        client, alice, content=ole2_signature + b"not really a workbook", account_id=account_id
    )

    assert upload["file_type"] == "xls"
    assert upload["status"] == "failed"
    assert upload["error_message"] == "file is not a readable .xls workbook"
    assert count_blobs(db) == 0


def test_a_file_that_failed_can_be_uploaded_again(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path
) -> None:
    """ADR 011 through the real flow: a failed import frees its file.

    The same bytes are sent twice. In real life a parser fix between the
    two attempts is what would make the second one succeed; here it is
    enough that the second is ACCEPTED (202, not 409) as a new upload.
    """
    broken = statement_bytes(
        tmp_path,
        rows=(("1", "01/08/2026", "01/08/2026", "", "UPI/SHOP/PAY", "seventy", "", "1.00"),),
    )
    first = import_file(client, alice, content=broken, account_id=account_id)
    assert first["status"] == "failed"

    second = post_upload(client, alice, content=broken, account_id=account_id)

    assert second.status_code == 202
    assert second.json()["id"] != first["id"]


def test_an_amount_too_large_to_store_is_rejected(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    """A misread cell can parse into a number no column can hold.

    Python integers have no upper bound, so `parse_amount_to_paise` returns
    whatever the cell says. Before the `MAX_PAISE` check in
    `app/parsers/base.py` this reached the INSERT, Postgres answered
    "numeric field overflow", and the user got a 500 with no row number -
    while the logged traceback carried the whole chunk's narrations and
    amounts (CLAUDE.md rule 3). It must be an ordinary bad row instead,
    which the worker records as a `failed` upload.
    """
    absurd = (
        ("1", "01/08/2026", "01/08/2026", "", "UPI/SHOP/PAY", "70.00", "0.00", "111471.33"),
        (
            "2",
            "02/08/2026",
            "02/08/2026",
            "",
            "UPI/SHOP/PAY",
            "99999999999999999999.00",
            "0.00",
            "111401.33",
        ),
    )

    upload = import_file(
        client, alice, content=statement_bytes(tmp_path, rows=absurd), account_id=account_id
    )

    assert upload["status"] == "failed"
    # Names the row, like every other unreadable-row error.
    assert "row 15" in upload["error_message"]
    assert "too large" in upload["error_message"]
    # Nothing stored, not even the good first row: the import is
    # all-or-nothing.
    assert count_transactions(db) == 0


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
    assert post_upload(client, alice, content=content, account_id=account_id).status_code == 202

    monkeypatch.setattr(imports_service, "_live_upload", lambda *_args: None)
    second = post_upload(client, alice, content=content, account_id=account_id)

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CONFLICT"


def test_an_integrity_error_that_is_not_a_duplicate_file_is_not_a_409(
    client: TestClient,
    alice: dict[str, str],
    account_id: str,
    db: Session,
    sync_queue: Queue,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the test above: only the file-hash constraint is a 409.

    The race it stands in for is a real one - the account is deleted (a
    second tab, or DELETE /me) after the ownership check and before the
    INSERT, so the upload row points at an account that no longer exists.
    Staging it needs the ownership check to hand back an account the
    database does not have, which is what the stub below does; the
    IntegrityError itself then comes from Postgres, with the real
    constraint name our mapping reads.

    It must stay a 500. A 409 here would tell the user "you already
    uploaded this file" about a file they have never uploaded.
    """
    owned = db.scalars(select(Account).where(Account.id == uuid.UUID(account_id))).one()
    vanished = Account(
        id=uuid.uuid4(),  # no such row
        user_id=owned.user_id,
        bank_code=owned.bank_code,
        nickname=owned.nickname,
        account_type=owned.account_type,
        masked_number=owned.masked_number,
    )
    monkeypatch.setattr(imports_service.accounts_service, "get_account", lambda *_args: vanished)

    with pytest.raises(IntegrityError) as excinfo:
        imports_service.create_upload(
            db,
            owned.user_id,
            account_id=vanished.id,
            filename="statement.xls",
            content=statement_bytes(tmp_path),
            queue=sync_queue,
        )

    assert excinfo.value.orig is not None
    assert excinfo.value.orig.diag.constraint_name == "fk_statement_uploads_account_id_accounts"
    # Rolled back: neither the row nor the encrypted file was kept, so
    # nothing was enqueued for it either.
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(StatementUpload)) == 0
    assert count_blobs(db) == 0


# --- Generic CSV + column mapping -------------------------------------------


def test_a_generic_csv_is_imported_with_a_column_mapping(
    client: TestClient, alice: dict[str, str], account_id: str, db: Session
) -> None:
    """The mapping travels to the worker through the database, not the job.

    A job's only argument is the upload id (SPEC §11), so the mapping is
    stored on the upload row (`column_mapping`, migration 0008) and the
    worker reads it from there.
    """
    body = import_file(
        client,
        alice,
        content=GENERIC_CSV,
        account_id=account_id,
        filename="export.csv",
        column_mapping=CSV_MAPPING,
    )

    assert body["status"] == "completed"
    assert body["file_type"] == "csv"
    assert (body["rows_parsed"], body["rows_inserted"]) == (2, 2)
    # A bare CSV states no period, so it is taken from the rows themselves.
    assert (body["period_start"], body["period_end"]) == ("2026-08-01", "2026-08-03")

    rows = transactions_of(db, body["id"])
    assert [(row.amount_paise, row.direction) for row in rows] == [
        (7000, Direction.DEBIT),
        (5000000, Direction.CREDIT),
    ]
    upload = db.get(StatementUpload, uuid.UUID(body["id"]))
    assert upload is not None
    assert upload.column_mapping == {
        "date_column": "Date",
        "description_column": "Narration",
        "date_format": "%d/%m/%Y",
        "debit_column": "Withdrawal",
        "credit_column": "Deposit",
        "amount_column": None,
        "balance_column": "Balance",
    }


def test_a_csv_without_a_mapping_fails(
    client: TestClient, alice: dict[str, str], account_id: str
) -> None:
    """No bank parser recognises a generic CSV, and without a mapping there
    is no way to know which column is which. The message says what to send."""
    upload = import_file(
        client, alice, content=GENERIC_CSV, account_id=account_id, filename="export.csv"
    )

    assert upload["status"] == "failed"
    assert upload["error_message"] == imports_service.NO_PARSER_MESSAGE
    assert "column_mapping" in upload["error_message"]


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
    upload = import_file(
        client,
        alice,
        content=statement_bytes(tmp_path),
        account_id=account_id,
        column_mapping=CSV_MAPPING,
    )

    assert upload["status"] == "completed"
    assert upload["rows_inserted"] == 5


def test_a_mapping_cannot_rescue_an_unrecognised_workbook(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path
) -> None:
    """A mapping names columns of a CSV table, so it cannot be applied to a
    `.xls` no parser recognises.

    The file is a real workbook (so the reader accepts it) with header
    labels no parser knows (so `detect()` finds nothing).
    """
    unknown_bank = statement_bytes(tmp_path, header_labels=("A", "B", "C", "D", "E", "F", "G", "H"))

    upload = import_file(
        client,
        alice,
        content=unknown_bank,
        account_id=account_id,
        column_mapping=CSV_MAPPING,
    )

    assert upload["status"] == "failed"
    # The exact message, not just "CSV": NO_PARSER_MESSAGE also contains the
    # word ("if it is a CSV, send a column_mapping"), so a looser assertion
    # would pass even if the two checks in parse_statement were reordered
    # and this file took the wrong branch.
    assert upload["error_message"] == imports_service.MAPPING_NEEDS_CSV_MESSAGE


def test_an_unrecognised_workbook_without_a_mapping_fails(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path
) -> None:
    unknown_bank = statement_bytes(tmp_path, header_labels=("A", "B", "C", "D", "E", "F", "G", "H"))

    upload = import_file(client, alice, content=unknown_bank, account_id=account_id)

    assert upload["status"] == "failed"
    assert upload["error_message"] == imports_service.NO_PARSER_MESSAGE


# --- Privacy (CLAUDE.md rule 3) ---------------------------------------------


def test_the_log_has_no_narrations_or_amounts(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path
) -> None:
    """An import logs IDs, the file type and counts - nothing else.

    `capture_logs` collects every structlog event raised during the
    request. The job runs inside the request on the test queue, so this
    covers the API side, the worker side, the request logger and anything
    else on the way.
    """
    content = statement_bytes(tmp_path)

    with structlog.testing.capture_logs() as events:
        response = post_upload(client, alice, content=content, account_id=account_id)

    assert response.status_code == 202
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

    names = [event["event"] for event in events]
    assert names.count("statement_upload_queued") == 1
    imported = [event for event in events if event["event"] == "statement_imported"]
    assert len(imported) == 1
    assert imported[0]["rows_inserted"] == 5


def test_a_failed_import_logs_a_reason_code_not_the_message(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path
) -> None:
    """The failure is logged as a fixed code. The user-facing message,
    which names a row of the file, goes only into `error_message`."""
    broken = (("1", "01/08/2026", "01/08/2026", "", "UPI/SECRETSHOP/PAY", "x", "", "1.00"),)

    with structlog.testing.capture_logs() as events:
        post_upload(
            client, alice, content=statement_bytes(tmp_path, rows=broken), account_id=account_id
        )

    failed = [event for event in events if event["event"] == "statement_import_failed"]
    assert len(failed) == 1
    assert failed[0]["reason"] == "invalid_statement"
    assert "SECRETSHOP" not in str(events)
    assert "row " not in str(failed[0])


# --- Large statements (more than one INSERT chunk) --------------------------

# One row more than `_INSERT_CHUNK_SIZE`, so the import needs exactly two
# INSERTs and the second one carries a single row.
#
# 501 and not 5000: one row past the boundary is the whole proof. If the
# slice arithmetic in `store_transactions` were wrong, the second chunk
# would be empty or short and the count would come back 500, so
# `501 != 500` catches it. A bigger statement only makes the suite slower.
ROWS_JUST_OVER_ONE_CHUNK = imports_service._INSERT_CHUNK_SIZE + 1


def test_a_statement_larger_than_one_chunk_is_fully_imported(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    """A statement too big for one INSERT still imports every row.

    Postgres accepts at most 65535 bind parameters per statement and each
    row binds about 20, so `store_transactions` splits the INSERT into
    chunks of `_INSERT_CHUNK_SIZE`. Every other test here imports five
    rows, which means `range(0, 5, 500)` yields one value and the loop
    body has only ever run once - the "there is a second chunk" path is
    untested.

    What makes that worth a test: a wrong slice (`values[start:SIZE]`
    instead of `values[start : start + SIZE]`) would import every
    statement under 500 rows perfectly, keep the suite green, and
    silently drop everything past row 500 of a real year-long statement -
    while still reporting success, because `rows_inserted` is counted from
    what the INSERT returned.

    The count is read from the service's own constant, so the test follows
    if that number is ever tuned.
    """
    rows = many_rows(ROWS_JUST_OVER_ONE_CHUNK)

    body = import_file(
        client,
        alice,
        content=statement_bytes(
            tmp_path, rows=rows, period_from="01/08/2026", period_to="31/12/2026"
        ),
        account_id=account_id,
    )

    assert body["status"] == "completed"
    assert (body["rows_parsed"], body["rows_inserted"], body["rows_duplicate"]) == (
        ROWS_JUST_OVER_ONE_CHUNK,
        ROWS_JUST_OVER_ONE_CHUNK,
        0,
    )
    # Counted in the database as well as reported in the response: the two
    # could disagree if the counting were wrong rather than the inserting.
    assert count_transactions(db) == ROWS_JUST_OVER_ONE_CHUNK


def test_a_large_statement_reimports_as_all_duplicates(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    """The same rows again: every one recognised, across both chunks.

    `ON CONFLICT DO NOTHING` is per INSERT, so dedupe has to hold for each
    chunk separately. The second file states a different period, which
    changes its bytes and therefore its sha256 - without that, dedupe
    level 1 would reject it before a single row was looked at. The header
    period is not part of a row's fingerprint, so the rows still hash
    identically.
    """
    rows = many_rows(ROWS_JUST_OVER_ONE_CHUNK)
    first = import_file(
        client,
        alice,
        content=statement_bytes(
            tmp_path, rows=rows, period_from="01/08/2026", period_to="31/12/2026"
        ),
        account_id=account_id,
    )
    assert first["status"] == "completed"

    body = import_file(
        client,
        alice,
        content=statement_bytes(
            tmp_path,
            name="again.xls",
            rows=rows,
            period_from="01/08/2026",
            period_to="01/01/2027",
        ),
        account_id=account_id,
        filename="again.xls",
    )

    assert (body["rows_parsed"], body["rows_inserted"], body["rows_duplicate"]) == (
        ROWS_JUST_OVER_ONE_CHUNK,
        0,
        ROWS_JUST_OVER_ONE_CHUNK,
    )
    assert count_transactions(db) == ROWS_JUST_OVER_ONE_CHUNK


# --- Statement order and balance reconciliation (SPEC §6.4, ADR 014) ------

# Three debits on one day, oldest first, with balances that add up.
SAME_DAY_ROWS = (
    ("1", "05/08/2026", "05/08/2026", "", "UPI/FIRST/PAY", "10.00", "0.00", "990.00"),
    ("2", "05/08/2026", "05/08/2026", "", "UPI/SECOND/PAY", "20.00", "0.00", "970.00"),
    ("3", "05/08/2026", "05/08/2026", "", "UPI/THIRD/PAY", "30.00", "0.00", "940.00"),
)


def test_a_statement_that_adds_up_is_completed_and_says_it_was_checked(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path
) -> None:
    upload = import_file(client, alice, content=statement_bytes(tmp_path), account_id=account_id)

    assert upload["status"] == "completed"
    assert upload["reconciliation_errors"] == 0
    # [] = checked, nothing wrong (null would mean "could not check").
    assert upload["reconciliation_issues"] == []


def test_a_statement_that_does_not_add_up_needs_review_and_names_the_row(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    """The second row's balance is misprinted (₹10 too high). The data is
    still imported - the user decides - but the upload says which rows to
    look at: the bad row, and the one after it, whose link to the bad
    value breaks as well. Row numbers point into the file."""
    broken = (
        ("1", "01/08/2026", "01/08/2026", "", "UPI/SHOP/PAY", "70.00", "0.00", "1000.00"),
        ("2", "02/08/2026", "02/08/2026", "", "UPI/SHOP/PAY", "20.00", "0.00", "990.00"),
        ("3", "03/08/2026", "03/08/2026", "", "UPI/SHOP/PAY", "30.00", "0.00", "950.00"),
    )

    upload = import_file(
        client, alice, content=statement_bytes(tmp_path, rows=broken), account_id=account_id
    )

    assert upload["status"] == "needs_review"
    assert upload["rows_inserted"] == 3
    assert upload["reconciliation_errors"] == 2
    assert upload["reconciliation_issues"] == [
        {"row_number": 15, "kind": "balance_mismatch"},
        {"row_number": 16, "kind": "balance_mismatch"},
    ]
    assert count_transactions(db) == 3
    # No amounts or balances in what is stored or returned (rule 3).
    assert "990" not in str(upload["reconciliation_issues"])


def test_a_statement_needing_review_still_blocks_the_same_file(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path
) -> None:
    """Its rows ARE imported, so uploading the same bytes again would only
    produce duplicates (ADR 011): 409, as for a completed upload."""
    broken = (
        ("1", "01/08/2026", "01/08/2026", "", "UPI/SHOP/PAY", "70.00", "0.00", "1000.00"),
        ("2", "02/08/2026", "02/08/2026", "", "UPI/SHOP/PAY", "20.00", "0.00", "1.00"),
    )
    content = statement_bytes(tmp_path, rows=broken)
    assert import_file(client, alice, content=content, account_id=account_id)["status"] == (
        "needs_review"
    )

    again = post_upload(client, alice, content=content, account_id=account_id)

    assert again.status_code == 409
    assert again.json()["error"]["message"] == imports_service.DUPLICATE_FILE_MESSAGE


def test_a_csv_without_a_balance_column_is_not_checked(
    client: TestClient, alice: dict[str, str], account_id: str
) -> None:
    """null, not []: nothing could be checked, so nothing is claimed."""
    mapping = (
        '{"date_column": "Date", "description_column": "Narration", '
        '"date_format": "%d/%m/%Y", "debit_column": "Withdrawal", "credit_column": "Deposit"}'
    )

    upload = import_file(
        client,
        alice,
        content=GENERIC_CSV,
        account_id=account_id,
        filename="export.csv",
        column_mapping=mapping,
    )

    assert upload["status"] == "completed"
    assert upload["reconciliation_issues"] is None
    assert upload["reconciliation_errors"] == 0


def test_the_statement_order_is_stored_oldest_first(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    upload = import_file(
        client, alice, content=statement_bytes(tmp_path, rows=SAME_DAY_ROWS), account_id=account_id
    )

    rows = db.scalars(
        select(Transaction)
        .where(Transaction.upload_id == uuid.UUID(upload["id"]))
        .order_by(Transaction.statement_seq)
    ).all()
    assert [(row.statement_seq, row.raw_description) for row in rows] == [
        (1, "UPI/FIRST/PAY"),
        (2, "UPI/SECOND/PAY"),
        (3, "UPI/THIRD/PAY"),
    ]


def test_a_newest_first_statement_is_stored_oldest_first_and_reconciles(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    """The same three days, exported newest first. Read as printed, every
    balance link would be broken; put in time order, it adds up - and the
    stored positions mean the same as for an oldest-first file."""
    newest_first = (
        ("1", "07/08/2026", "07/08/2026", "", "UPI/THIRD/PAY", "30.00", "0.00", "940.00"),
        ("2", "06/08/2026", "06/08/2026", "", "UPI/SECOND/PAY", "20.00", "0.00", "970.00"),
        ("3", "05/08/2026", "05/08/2026", "", "UPI/FIRST/PAY", "10.00", "0.00", "990.00"),
    )

    upload = import_file(
        client, alice, content=statement_bytes(tmp_path, rows=newest_first), account_id=account_id
    )

    assert upload["status"] == "completed"
    assert upload["reconciliation_issues"] == []
    rows = db.scalars(select(Transaction).order_by(Transaction.statement_seq)).all()
    assert [row.raw_description for row in rows] == [
        "UPI/FIRST/PAY",
        "UPI/SECOND/PAY",
        "UPI/THIRD/PAY",
    ]


def test_one_days_transactions_are_listed_in_statement_order(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path
) -> None:
    """The bug this step exists for: same-day rows came back in random
    order (ordered by a random UUID), so the balance column jumped around.
    Newest first means the day's LAST transaction first, and each balance
    then follows from the one below it."""
    import_file(
        client, alice, content=statement_bytes(tmp_path, rows=SAME_DAY_ROWS), account_id=account_id
    )

    items = client.get("/api/v1/transactions", headers=alice).json()["items"]

    assert [item["raw_description"] for item in items] == [
        "UPI/THIRD/PAY",
        "UPI/SECOND/PAY",
        "UPI/FIRST/PAY",
    ]


def test_a_one_day_newest_first_statement_is_completed(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    """Found in the Step 4 review. The parser used to derive an opening
    balance from the FIRST row of the file, which for a newest-first export
    of a single day is the day's LAST transaction - so a correct statement
    came out `needs_review`. Now nothing is derived, the day is ordered by
    its balance chain, and it adds up."""
    newest_first = tuple(reversed(SAME_DAY_ROWS))

    upload = import_file(
        client, alice, content=statement_bytes(tmp_path, rows=newest_first), account_id=account_id
    )

    assert upload["status"] == "completed"
    assert upload["reconciliation_issues"] == []
    rows = db.scalars(select(Transaction).order_by(Transaction.statement_seq)).all()
    assert [row.raw_description for row in rows] == [
        "UPI/FIRST/PAY",
        "UPI/SECOND/PAY",
        "UPI/THIRD/PAY",
    ]
