"""GET /transactions and PATCH /transactions/{id} end to end.

Transactions are inserted directly with the ORM here, not by uploading a
statement: these tests are about the ENDPOINT, so they need exact control
over dates, amounts and narrations. The import path itself is covered by
tests/integration/test_uploads.py.

The tests that matter most, and the rules they defend:

- `test_paging_returns_every_row_exactly_once`        keyset pagination
- `test_paging_through_rows_on_the_same_day_...`      the id tie-breaker
- `test_a_literal_percent_is_not_a_wildcard`          ILIKE escaping
- `test_another_users_transaction_is_not_found`       rule 2 (IDOR)
- `test_another_users_category_cannot_be_used`        rule 2 (IDOR)
- `test_a_cursor_never_reaches_another_users_rows`    rule 2 (IDOR)
- `test_a_transfer_category_marks_the_row_as_...`     SPEC §9.1 totals
- `test_a_blank_filter_is_not_a_filter`               blank = not sent
"""

import base64
import datetime
import itertools
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.seed import run_seed
from app.models import (
    Category,
    CategoryKind,
    CategorySource,
    StatementUpload,
    Transaction,
    UploadStatus,
    User,
)
from app.parsers.base import Direction, FileType
from app.utils.cursor import encode_cursor

pytestmark = pytest.mark.integration

TRANSACTIONS = "/api/v1/transactions"
ACCOUNTS = "/api/v1/accounts"

AuthHeadersFactory = Callable[[str], dict[str, str]]

AUGUST = datetime.date(2026, 8, 1)


# --- Fixtures and helpers ---------------------------------------------------


@dataclass(frozen=True)
class Owner:
    """One logged-in user with an account and an upload to hang rows on."""

    email: str
    headers: dict[str, str]
    user_id: uuid.UUID
    account_id: uuid.UUID
    upload_id: uuid.UUID


def make_owner(
    client: TestClient, db: Session, make_auth_headers: AuthHeadersFactory, email: str
) -> Owner:
    """Register a user through the API, then give them an account and upload.

    The account goes through the real endpoint (so it is built exactly as a
    user's would be); the `statement_uploads` row is inserted directly,
    because `transactions.upload_id` is NOT NULL and importing a real file
    here would only slow the test down.
    """
    headers = make_auth_headers(email)
    response = client.post(
        ACCOUNTS,
        json={
            "bank_code": "ICICI",
            "nickname": "Salary account",
            "account_type": "savings",
            "masked_number": "1234",
        },
        headers=headers,
    )
    assert response.status_code == 201
    account_id = uuid.UUID(response.json()["id"])

    user_id = db.scalars(select(User.id).where(User.email == email)).one()
    upload = StatementUpload(
        user_id=user_id,
        account_id=account_id,
        original_filename="statement.xls",
        file_type=FileType.XLS,
        # Unique per user among live uploads (the file-hash index, ADR 011),
        # and one upload per user here.
        file_sha256=uuid.uuid4().hex * 2,
        status=UploadStatus.COMPLETED,
    )
    db.add(upload)
    db.commit()
    return Owner(
        email=email,
        headers=headers,
        user_id=user_id,
        account_id=account_id,
        upload_id=upload.id,
    )


@pytest.fixture
def alice(client: TestClient, db: Session, make_auth_headers: AuthHeadersFactory) -> Owner:
    return make_owner(client, db, make_auth_headers, "alice@example.com")


@pytest.fixture
def bob(client: TestClient, db: Session, make_auth_headers: AuthHeadersFactory) -> Owner:
    return make_owner(client, db, make_auth_headers, "bob@example.com")


# Positions for the rows these tests insert by hand, counting up. Every row
# here belongs to its owner's one upload, where a position may be used once.
_SEQUENCE = itertools.count(1)


def add_transaction(
    db: Session,
    owner: Owner,
    *,
    txn_date: datetime.date,
    amount_paise: int,
    direction: Direction = Direction.DEBIT,
    description: str = "UPI/SHOP/PAY",
    account_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
) -> Transaction:
    """Insert one transaction row for this owner and return it.

    The fingerprint only has to be unique per account here (the real one is
    a sha256 of the row's fields, see ADR 006), so a random 64-character
    hex string keeps every inserted row distinct.
    """
    transaction = Transaction(
        user_id=owner.user_id,
        account_id=account_id or owner.account_id,
        upload_id=owner.upload_id,
        txn_date=txn_date,
        amount_paise=amount_paise,
        direction=direction,
        raw_description=description,
        # The importer stores the normalized narration uppercased
        # (SPEC §6.5); `q=` searches that column.
        normalized_description=description.upper(),
        category_id=category_id,
        fingerprint=uuid.uuid4().hex * 2,
        # Unique per upload (migration 0010); this order is insertion order.
        statement_seq=next(_SEQUENCE),
    )
    db.add(transaction)
    db.commit()
    return transaction


def get_page(
    client: TestClient, owner: Owner, **params: Any
) -> tuple[list[dict[str, Any]], str | None]:
    """Call GET /transactions and return (items, next_cursor)."""
    response = client.get(TRANSACTIONS, params=params, headers=owner.headers)
    assert response.status_code == 200, response.text
    body = response.json()
    return body["items"], body["next_cursor"]


def descriptions(items: list[dict[str, Any]]) -> list[str]:
    return [item["raw_description"] for item in items]


# The sample statement used by the filter tests. Dates ascend, so the
# endpoint (newest first) returns them bottom-up.
SAMPLE_ROWS: tuple[tuple[int, str, int, Direction], ...] = (
    (1, "UPI/CHAI SHOP/PAY", 7_000, Direction.DEBIT),  # 1 Aug, Rs 70
    (2, "UPI/ZOMATO ONLINE/PAY", 125_050, Direction.DEBIT),  # 2 Aug, Rs 1250.50
    (3, "NEFT-SALARY-ACME", 5_000_000, Direction.CREDIT),  # 3 Aug, Rs 50000
    (4, "ATM CASH WDL 50%FEE", 50_000, Direction.DEBIT),  # 4 Aug, Rs 500
    (5, "ANNUAL 50 CARD FEE", 20_000, Direction.DEBIT),  # 5 Aug, Rs 200
)


@pytest.fixture
def sample(db: Session, alice: Owner) -> Owner:
    """Five of Alice's transactions, one per day from 1 to 5 August."""
    for day, description, amount_paise, direction in SAMPLE_ROWS:
        add_transaction(
            db,
            alice,
            txn_date=AUGUST.replace(day=day),
            amount_paise=amount_paise,
            direction=direction,
            description=description,
        )
    return alice


@pytest.fixture
def system_categories(db: Session) -> dict[str, Category]:
    """The seeded system categories, by slug (the `db` fixture truncates)."""
    run_seed(db)
    return {category.slug: category for category in db.scalars(select(Category))}


# --- Auth -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", TRANSACTIONS), ("patch", f"{TRANSACTIONS}/{uuid.uuid4()}")],
)
def test_every_route_needs_a_token(client: TestClient, method: str, path: str) -> None:
    response = client.request(method, path, json={})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


# --- Listing ----------------------------------------------------------------


def test_a_user_with_no_transactions_gets_an_empty_page(client: TestClient, alice: Owner) -> None:
    items, next_cursor = get_page(client, alice)

    assert items == []
    assert next_cursor is None


def test_transactions_come_back_newest_first(client: TestClient, sample: Owner) -> None:
    items, next_cursor = get_page(client, sample)

    assert [item["txn_date"] for item in items] == [
        "2026-08-05",
        "2026-08-04",
        "2026-08-03",
        "2026-08-02",
        "2026-08-01",
    ]
    # Five rows and a default limit of 50: one page, so no cursor.
    assert next_cursor is None


def test_the_response_describes_one_transaction_fully(
    client: TestClient, db: Session, alice: Owner
) -> None:
    transaction = add_transaction(
        db,
        alice,
        txn_date=AUGUST,
        amount_paise=12_50_000,  # Rs 12,500.00
        description="UPI/ZOMATO/PAY",
    )

    items, _ = get_page(client, alice)

    assert items == [
        {
            "id": str(transaction.id),
            "account_id": str(alice.account_id),
            "upload_id": str(alice.upload_id),
            "txn_date": "2026-08-01",
            "value_date": None,
            "amount_paise": 1_250_000,
            # Indian grouping, from app/utils/money.py (rule 10).
            "amount_display": "₹12,500.00",
            "direction": "debit",
            "balance_after_paise": None,
            "raw_description": "UPI/ZOMATO/PAY",
            "normalized_description": "UPI/ZOMATO/PAY",
            # Phase 2 fills these in.
            "channel": "other",
            "counterparty_vpa": None,
            # Nothing has categorized this row yet.
            "category_id": None,
            "category_source": "none",
            "category_confidence": None,
            "is_self_transfer": False,
            "notes": None,
            "created_at": items[0]["created_at"],
        }
    ]


def test_only_your_own_transactions_are_listed(
    client: TestClient, db: Session, alice: Owner, bob: Owner
) -> None:
    add_transaction(db, alice, txn_date=AUGUST, amount_paise=100, description="ALICE ROW")
    add_transaction(db, bob, txn_date=AUGUST, amount_paise=100, description="BOB ROW")

    alice_items, _ = get_page(client, alice)
    bob_items, _ = get_page(client, bob)

    assert descriptions(alice_items) == ["ALICE ROW"]
    assert descriptions(bob_items) == ["BOB ROW"]


# --- Filters ----------------------------------------------------------------


def test_the_date_range_includes_both_ends(client: TestClient, sample: Owner) -> None:
    items, _ = get_page(client, sample, **{"from": "2026-08-02", "to": "2026-08-04"})

    assert descriptions(items) == [
        "ATM CASH WDL 50%FEE",
        "NEFT-SALARY-ACME",
        "UPI/ZOMATO ONLINE/PAY",
    ]


def test_direction_filters_money_in_from_money_out(client: TestClient, sample: Owner) -> None:
    credits, _ = get_page(client, sample, direction="credit")
    debits, _ = get_page(client, sample, direction="debit")

    assert descriptions(credits) == ["NEFT-SALARY-ACME"]
    assert len(debits) == 4


def test_amounts_are_filtered_in_rupees(client: TestClient, sample: Owner) -> None:
    # Rs 500 is 50000 paise: the ATM row matches exactly (inclusive bound).
    big, _ = get_page(client, sample, min_amount="500")
    small, _ = get_page(client, sample, max_amount="200")
    middle, _ = get_page(client, sample, min_amount="200", max_amount="1250.50")

    assert descriptions(big) == [
        "ATM CASH WDL 50%FEE",
        "NEFT-SALARY-ACME",
        "UPI/ZOMATO ONLINE/PAY",
    ]
    assert descriptions(small) == ["ANNUAL 50 CARD FEE", "UPI/CHAI SHOP/PAY"]
    # 1250.50 rupees is 125050 paise exactly - no float rounding (rule 1).
    assert descriptions(middle) == [
        "ANNUAL 50 CARD FEE",
        "ATM CASH WDL 50%FEE",
        "UPI/ZOMATO ONLINE/PAY",
    ]


def test_the_search_is_case_insensitive_and_matches_anywhere(
    client: TestClient, sample: Owner
) -> None:
    items, _ = get_page(client, sample, q="zomato")

    assert descriptions(items) == ["UPI/ZOMATO ONLINE/PAY"]


def test_a_literal_percent_is_not_a_wildcard(client: TestClient, sample: Owner) -> None:
    """`%` and `_` in the search text mean themselves, not "anything".

    Without escaping, "50%FEE" would match "ANNUAL 50 CARD FEE" too (`%` =
    any text) and "50_CARD" would match "50 CARD" (`_` = any character).
    """
    percent, _ = get_page(client, sample, q="50%FEE")
    underscore, _ = get_page(client, sample, q="50_CARD")
    plain, _ = get_page(client, sample, q="50")

    assert descriptions(percent) == ["ATM CASH WDL 50%FEE"]
    assert descriptions(underscore) == []
    # Both rows contain "50" somewhere, so the search itself still works.
    assert descriptions(plain) == ["ANNUAL 50 CARD FEE", "ATM CASH WDL 50%FEE"]


def test_filters_combine_with_and(client: TestClient, sample: Owner) -> None:
    items, _ = get_page(client, sample, direction="debit", min_amount="100", **{"to": "2026-08-04"})

    assert descriptions(items) == ["ATM CASH WDL 50%FEE", "UPI/ZOMATO ONLINE/PAY"]


def test_the_account_filter_only_shows_that_account(
    client: TestClient, db: Session, alice: Owner
) -> None:
    second = client.post(
        ACCOUNTS,
        json={
            "bank_code": "GENERIC",
            "nickname": "Second account",
            "account_type": "savings",
            "masked_number": "9999",
        },
        headers=alice.headers,
    )
    assert second.status_code == 201
    other_account_id = uuid.UUID(second.json()["id"])
    add_transaction(db, alice, txn_date=AUGUST, amount_paise=100, description="FIRST ACCOUNT ROW")
    add_transaction(
        db,
        alice,
        txn_date=AUGUST,
        amount_paise=100,
        description="SECOND ACCOUNT ROW",
        account_id=other_account_id,
    )

    items, _ = get_page(client, alice, account_id=str(other_account_id))

    assert descriptions(items) == ["SECOND ACCOUNT ROW"]


def test_the_category_filter_only_shows_that_category(
    client: TestClient, db: Session, alice: Owner, system_categories: dict[str, Category]
) -> None:
    groceries = system_categories["groceries"]
    add_transaction(
        db,
        alice,
        txn_date=AUGUST,
        amount_paise=100,
        description="GROCERY ROW",
        category_id=groceries.id,
    )
    add_transaction(db, alice, txn_date=AUGUST, amount_paise=100, description="UNCATEGORIZED ROW")

    items, _ = get_page(client, alice, category_id=str(groceries.id))

    assert descriptions(items) == ["GROCERY ROW"]


def test_another_users_account_is_not_found(client: TestClient, alice: Owner, bob: Owner) -> None:
    """Filtering by someone else's account id is a 404, not an empty page.

    An empty page is a legitimate answer ("you have nothing there"), so
    answering it here would hide the mistake from the client - while still
    telling them the id exists is exactly what we must not do (rule 2).
    """
    response = client.get(
        TRANSACTIONS, params={"account_id": str(bob.account_id)}, headers=alice.headers
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_another_users_category_is_not_found(
    client: TestClient, db: Session, alice: Owner, bob: Owner
) -> None:
    bobs_category = Category(
        user_id=bob.user_id, name="Bob's budget", slug="bobs-budget", kind=CategoryKind.EXPENSE
    )
    db.add(bobs_category)
    db.commit()

    response = client.get(
        TRANSACTIONS, params={"category_id": str(bobs_category.id)}, headers=alice.headers
    )

    assert response.status_code == 404


def test_an_unknown_category_is_not_found(client: TestClient, alice: Owner) -> None:
    response = client.get(
        TRANSACTIONS, params={"category_id": str(uuid.uuid4())}, headers=alice.headers
    )

    assert response.status_code == 404


# --- Query validation -------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"from": "2026-09-01", "to": "2026-08-01"}, "query"),
        ({"min_amount": "900", "max_amount": "100"}, "query"),
        ({"limit": "0"}, "query.limit"),
        ({"limit": "201"}, "query.limit"),
        ({"min_amount": "-1"}, "query.min_amount"),
        ({"min_amount": "10.123"}, "query.min_amount"),
        ({"direction": "sideways"}, "query.direction"),
        ({"from": "not-a-date"}, "query.from"),
        ({"cursor": "not-a-cursor"}, "query.cursor"),
        ({"account_id": "not-a-uuid"}, "query.account_id"),
        # A misspelled filter must not be ignored: it would silently return
        # everything, which looks like the API is broken.
        ({"catgory_id": str(uuid.uuid4())}, "query.catgory_id"),
    ],
)
def test_invalid_query_parameters_are_rejected(
    client: TestClient, alice: Owner, params: dict[str, str], field: str
) -> None:
    response = client.get(TRANSACTIONS, params=params, headers=alice.headers)

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert [detail["field"] for detail in body["error"]["details"]] == [field]


def test_a_blank_search_is_not_a_filter(client: TestClient, sample: Owner) -> None:
    """`?q=` is a cleared search box, so it must not filter anything out."""
    items, _ = get_page(client, sample, q="   ")

    assert len(items) == len(SAMPLE_ROWS)


@pytest.mark.parametrize(
    "blank",
    [
        {"from": ""},
        {"to": ""},
        {"direction": ""},
        {"account_id": ""},
        {"category_id": ""},
        {"min_amount": ""},
        {"max_amount": ""},
        {"cursor": ""},
        {"limit": ""},
        # What a form that serializes every filter field actually sends.
        {"from": "", "to": "", "direction": "", "q": "", "min_amount": "", "limit": ""},
    ],
)
def test_a_blank_filter_is_not_a_filter(
    client: TestClient, sample: Owner, blank: dict[str, str]
) -> None:
    """Every filter treats an empty value as "not sent", not as a 422.

    A client that builds the query string from all of its filter inputs
    sends an empty value for the ones the user left alone. Before this,
    `?q=` worked while `?from=` and `?direction=` were 422s - the same
    request accepted or rejected depending on which box was left blank.
    """
    items, _ = get_page(client, sample, **blank)

    assert len(items) == len(SAMPLE_ROWS)


def test_a_blank_unknown_parameter_is_still_rejected(client: TestClient, alice: Owner) -> None:
    """Dropping blanks must not start forgiving misspelled filters."""
    response = client.get(TRANSACTIONS, params={"catgory_id": ""}, headers=alice.headers)

    assert response.status_code == 422
    assert [d["field"] for d in response.json()["error"]["details"]] == ["query.catgory_id"]


# --- Keyset pagination ------------------------------------------------------


def test_paging_returns_every_row_exactly_once(client: TestClient, sample: Owner) -> None:
    """Walk the whole list two rows at a time: no gaps, no repeats.

    This is the test the cursor exists for. It also proves the cursor is
    self-contained: each page is requested with nothing but the limit and
    the cursor the previous page returned.
    """
    expected, _ = get_page(client, sample)
    expected_ids = [item["id"] for item in expected]

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        items, cursor = get_page(client, sample, **params)
        seen.extend(item["id"] for item in items)
        pages += 1
        if cursor is None:
            break
        assert pages < 10, "the cursor never ended - paging is looping"

    # 5 rows at 2 per page: 2 + 2 + 1.
    assert pages == 3
    assert seen == expected_ids
    assert len(set(seen)) == len(seen)


def test_the_last_page_has_no_cursor(client: TestClient, sample: Owner) -> None:
    """A full last page must still end the walk.

    With exactly `limit` rows left, the service's "read one extra row"
    trick finds nothing extra, so there is no next page - without that,
    the client would ask for one more page and get an empty one.
    """
    items, cursor = get_page(client, sample, limit=5)

    assert len(items) == 5
    assert cursor is None


def test_paging_through_rows_on_the_same_day_is_stable(
    client: TestClient, db: Session, alice: Owner
) -> None:
    """Four transactions on ONE day, read one per page.

    The date alone cannot order these, so the cursor also carries the id
    (the second half of the sort key). Without it, each page would start at
    the same date again and either repeat or skip rows.
    """
    for index in range(4):
        add_transaction(db, alice, txn_date=AUGUST, amount_paise=100 + index)

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(4):
        params: dict[str, Any] = {"limit": 1}
        if cursor is not None:
            params["cursor"] = cursor
        items, cursor = get_page(client, alice, **params)
        assert len(items) == 1
        seen.append(items[0]["id"])

    assert cursor is None
    assert len(set(seen)) == 4


def test_filters_still_apply_on_later_pages(client: TestClient, sample: Owner) -> None:
    first, cursor = get_page(client, sample, direction="debit", limit=2)
    assert cursor is not None

    second, cursor = get_page(client, sample, direction="debit", limit=2, cursor=cursor)

    assert descriptions(first) == ["ANNUAL 50 CARD FEE", "ATM CASH WDL 50%FEE"]
    # The credit row in the middle of the date range stays filtered out.
    assert descriptions(second) == ["UPI/ZOMATO ONLINE/PAY", "UPI/CHAI SHOP/PAY"]
    assert cursor is None


def test_a_cursor_never_reaches_another_users_rows(
    client: TestClient, db: Session, alice: Owner, bob: Owner
) -> None:
    """A cursor is a position, not a permission.

    Bob pages with a cursor built from Alice's row. The position is used,
    but the query is still `WHERE user_id = bob` (rule 2), so he sees his
    own older rows and nothing of hers.
    """
    alice_row = add_transaction(
        db, alice, txn_date=AUGUST.replace(day=3), amount_paise=100, description="ALICE ROW"
    )
    add_transaction(
        db, bob, txn_date=AUGUST.replace(day=2), amount_paise=100, description="BOB OLD"
    )
    add_transaction(
        db, bob, txn_date=AUGUST.replace(day=4), amount_paise=100, description="BOB NEW"
    )

    stolen_cursor = encode_cursor(alice_row.txn_date, alice_row.statement_seq, alice_row.id)
    items, _ = get_page(client, bob, cursor=stolen_cursor)

    assert descriptions(items) == ["BOB OLD"]


# --- PATCH: manual categorization -------------------------------------------


def test_a_transfer_category_marks_the_row_as_a_self_transfer(
    client: TestClient, db: Session, alice: Owner, system_categories: dict[str, Category]
) -> None:
    """Filing a row under "Self Transfer" must set the flag the insights read.

    SPEC §9.1 excludes self transfers from spending and income, but the
    insights queries read `is_self_transfer`, not the category. If the two
    could disagree, a credit-card payment the user correctly filed as a
    self transfer would still be counted as spending - and there is no
    `is_self_transfer` field in the PATCH body to fix it with.
    """
    transaction = add_transaction(db, alice, txn_date=AUGUST, amount_paise=20_00_000)
    self_transfer = system_categories["self-transfer"]
    assert self_transfer.kind is CategoryKind.TRANSFER

    response = client.patch(
        f"{TRANSACTIONS}/{transaction.id}",
        json={"category_id": str(self_transfer.id)},
        headers=alice.headers,
    )

    assert response.status_code == 200
    assert response.json()["is_self_transfer"] is True
    db.refresh(transaction)
    assert transaction.is_self_transfer is True


@pytest.mark.parametrize("second_body", [{"category_id": "groceries"}, {"category_id": None}])
def test_leaving_the_transfer_category_clears_the_self_transfer_flag(
    client: TestClient,
    db: Session,
    alice: Owner,
    system_categories: dict[str, Category],
    second_body: dict[str, str | None],
) -> None:
    """A stale True must not keep a row out of the totals invisibly.

    Re-filing the row as groceries, or clearing its category, both mean
    "this is not a transfer between my accounts".
    """
    transaction = add_transaction(
        db,
        alice,
        txn_date=AUGUST,
        amount_paise=20_00_000,
        category_id=system_categories["self-transfer"].id,
    )
    url = f"{TRANSACTIONS}/{transaction.id}"
    assert (
        client.patch(
            url,
            json={"category_id": str(system_categories["self-transfer"].id)},
            headers=alice.headers,
        ).json()["is_self_transfer"]
        is True
    )

    slug = second_body["category_id"]
    body = {"category_id": str(system_categories[slug].id) if slug is not None else None}
    response = client.patch(url, json=body, headers=alice.headers)

    assert response.status_code == 200
    assert response.json()["is_self_transfer"] is False


def test_a_note_alone_does_not_touch_the_self_transfer_flag(
    client: TestClient, db: Session, alice: Owner
) -> None:
    """Phase 2 will set this flag by detection; a note must not undo that."""
    transaction = add_transaction(db, alice, txn_date=AUGUST, amount_paise=20_00_000)
    transaction.is_self_transfer = True
    db.commit()

    response = client.patch(
        f"{TRANSACTIONS}/{transaction.id}",
        json={"notes": "moved to my card"},
        headers=alice.headers,
    )

    assert response.status_code == 200
    assert response.json()["is_self_transfer"] is True


def test_choosing_a_category_records_that_the_user_chose_it(
    client: TestClient, db: Session, alice: Owner, system_categories: dict[str, Category]
) -> None:
    transaction = add_transaction(db, alice, txn_date=AUGUST, amount_paise=7_000)
    groceries = system_categories["groceries"]

    response = client.patch(
        f"{TRANSACTIONS}/{transaction.id}",
        json={"category_id": str(groceries.id)},
        headers=alice.headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["category_id"] == str(groceries.id)
    # `user_manual` is what stops the Phase 2 categorizer overwriting it.
    assert body["category_source"] == "user_manual"
    assert body["category_confidence"] is None
    # An expense category is not a transfer between the user's accounts.
    assert body["is_self_transfer"] is False
    # Stored, not only echoed back.
    db.refresh(transaction)
    assert transaction.category_id == groceries.id
    assert transaction.category_source is CategorySource.USER_MANUAL


def test_clearing_the_category_makes_the_row_uncategorized_again(
    client: TestClient, db: Session, alice: Owner, system_categories: dict[str, Category]
) -> None:
    transaction = add_transaction(
        db,
        alice,
        txn_date=AUGUST,
        amount_paise=7_000,
        category_id=system_categories["groceries"].id,
    )

    response = client.patch(
        f"{TRANSACTIONS}/{transaction.id}", json={"category_id": None}, headers=alice.headers
    )

    assert response.status_code == 200
    assert response.json()["category_id"] is None
    # Back to "nothing has decided this", so the categorizer may look again.
    assert response.json()["category_source"] == "none"


def test_a_note_can_be_added_and_removed(client: TestClient, db: Session, alice: Owner) -> None:
    transaction = add_transaction(db, alice, txn_date=AUGUST, amount_paise=7_000)
    url = f"{TRANSACTIONS}/{transaction.id}"

    added = client.patch(url, json={"notes": "  gift for Mom  "}, headers=alice.headers)
    cleared = client.patch(url, json={"notes": ""}, headers=alice.headers)

    # Surrounding whitespace is stripped by the schema.
    assert added.json()["notes"] == "gift for Mom"
    # An empty note is stored as NULL, so "no note" has one representation.
    assert cleared.json()["notes"] is None


def test_a_field_that_was_not_sent_is_left_alone(
    client: TestClient, db: Session, alice: Owner, system_categories: dict[str, Category]
) -> None:
    """PATCH is partial: setting the note must not clear the category.

    This is why the service uses `model_dump(exclude_unset=True)` - without
    it, the `category_id` default of None would look like "clear it".
    """
    transaction = add_transaction(
        db,
        alice,
        txn_date=AUGUST,
        amount_paise=7_000,
        category_id=system_categories["groceries"].id,
    )

    response = client.patch(
        f"{TRANSACTIONS}/{transaction.id}", json={"notes": "cash paid back"}, headers=alice.headers
    )

    assert response.status_code == 200
    assert response.json()["category_id"] == str(system_categories["groceries"].id)
    assert response.json()["notes"] == "cash paid back"


def test_another_users_transaction_is_not_found(
    client: TestClient, db: Session, alice: Owner, bob: Owner
) -> None:
    bobs_transaction = add_transaction(db, bob, txn_date=AUGUST, amount_paise=7_000)

    response = client.patch(
        f"{TRANSACTIONS}/{bobs_transaction.id}", json={"notes": "mine now"}, headers=alice.headers
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    # And nothing was written.
    db.refresh(bobs_transaction)
    assert bobs_transaction.notes is None


def test_another_users_category_cannot_be_used(
    client: TestClient, db: Session, alice: Owner, bob: Owner
) -> None:
    """Alice may not file her transaction under Bob's private category.

    Allowing it would also leak: the category id (and later its name) would
    come back to her in every listing of that transaction.
    """
    transaction = add_transaction(db, alice, txn_date=AUGUST, amount_paise=7_000)
    bobs_category = Category(
        user_id=bob.user_id, name="Bob's budget", slug="bobs-budget", kind=CategoryKind.EXPENSE
    )
    db.add(bobs_category)
    db.commit()

    response = client.patch(
        f"{TRANSACTIONS}/{transaction.id}",
        json={"category_id": str(bobs_category.id)},
        headers=alice.headers,
    )

    assert response.status_code == 404
    db.refresh(transaction)
    assert transaction.category_id is None


def test_an_unknown_transaction_is_not_found(client: TestClient, alice: Owner) -> None:
    response = client.patch(
        f"{TRANSACTIONS}/{uuid.uuid4()}", json={"notes": "hello"}, headers=alice.headers
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("body", "field"),
    [
        # Phase 2 fields: rejected, not silently ignored.
        ({"is_self_transfer": True}, "body.is_self_transfer"),
        ({"apply_to_similar": True}, "body.apply_to_similar"),
        ({"notes": "x" * 501}, "body.notes"),
        # A NUL byte cannot be stored in a text column at all.
        ({"notes": "bad\u0000note"}, "body.notes"),
        ({"category_id": "not-a-uuid"}, "body.category_id"),
    ],
)
def test_invalid_patch_bodies_are_rejected(
    client: TestClient, db: Session, alice: Owner, body: dict[str, Any], field: str
) -> None:
    transaction = add_transaction(db, alice, txn_date=AUGUST, amount_paise=7_000)

    response = client.patch(f"{TRANSACTIONS}/{transaction.id}", json=body, headers=alice.headers)

    assert response.status_code == 422
    details = response.json()["error"]["details"]
    assert [detail["field"] for detail in details] == [field]


def test_an_empty_patch_changes_nothing(
    client: TestClient, db: Session, alice: Owner, system_categories: dict[str, Category]
) -> None:
    transaction = add_transaction(
        db,
        alice,
        txn_date=AUGUST,
        amount_paise=7_000,
        category_id=system_categories["salary"].id,
    )

    response = client.patch(f"{TRANSACTIONS}/{transaction.id}", json={}, headers=alice.headers)

    assert response.status_code == 200
    assert response.json()["category_id"] == str(system_categories["salary"].id)
    # Not touched, so the source the importer/categorizer set stays.
    assert response.json()["category_source"] == "none"


def test_one_days_rows_are_listed_by_their_statement_position(
    client: TestClient, db: Session, alice: Owner
) -> None:
    """Same day: the higher `statement_seq` (later in the statement) comes
    first, whatever order the ids happen to fall in (ADR 014)."""
    rows = [add_transaction(db, alice, txn_date=AUGUST, amount_paise=100 + i) for i in range(3)]

    items, _cursor = get_page(client, alice)

    assert [item["id"] for item in items] == [str(row.id) for row in reversed(rows)]


def test_a_cursor_from_before_statement_order_is_rejected(client: TestClient, alice: Owner) -> None:
    """Two-part cursors (txn_date|id) were issued before Phase 2 Step 4. A
    client still holding one gets a 422 and starts from page 1, instead of
    landing at a position the new sort key does not have."""
    old_style = base64.urlsafe_b64encode(f"2026-08-04|{uuid.uuid4()}".encode()).decode()

    response = client.get(TRANSACTIONS, params={"cursor": old_style}, headers=alice.headers)

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "query.cursor"
