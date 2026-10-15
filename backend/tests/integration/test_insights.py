"""GET /insights/summary and GET /insights/categories end to end.

Rows are inserted with the ORM rather than by uploading a statement: these
tests are about the aggregation, so they need exact control over dates,
amounts, directions and the self-transfer flag. The import path is covered
by tests/integration/test_uploads.py.

**Every expected number below is worked out by hand** in the comments next
to `AUGUST_ROWS` / `JULY_ROWS`, which is the point of the exercise - a test
that asserts whatever the SQL happens to return would pass just as happily
with a wrong query.

The tests that matter most, and the rules they defend:

- `test_a_self_transfer_is_not_income_and_not_spending`   SPEC §9.1
- `test_filing_a_row_as_a_self_transfer_removes_it_...`   the §9.1 + §8.4 link
- `test_another_users_transactions_are_not_in_my_...`     rule 2 (IDOR)
- `test_percent_change_is_null_when_last_month_was_empty` the "safe when 0" rule
- `test_the_parts_add_up_to_the_total`                    the uncategorized bucket
"""

import datetime
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.seed import run_seed
from app.models import Category, StatementUpload, Transaction, UploadStatus, User
from app.parsers.base import Direction, FileType
from app.utils.dates import Month

pytestmark = pytest.mark.integration

SUMMARY = "/api/v1/insights/summary"
CATEGORIES = "/api/v1/insights/categories"
TRANSACTIONS = "/api/v1/transactions"
ACCOUNTS = "/api/v1/accounts"

AuthHeadersFactory = Callable[[str], dict[str, str]]


# --- Fixtures and helpers ---------------------------------------------------


@dataclass(frozen=True)
class Owner:
    """One logged-in user with an account and an upload to hang rows on."""

    headers: dict[str, str]
    user_id: uuid.UUID
    account_id: uuid.UUID
    upload_id: uuid.UUID


def make_owner(
    client: TestClient, db: Session, make_auth_headers: AuthHeadersFactory, email: str
) -> Owner:
    """Register a user through the API, then give them an account and upload.

    The account goes through the real endpoint; the `statement_uploads` row
    is inserted directly, because `transactions.upload_id` is NOT NULL and
    importing a real file here would only slow the test down.
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
    return Owner(headers=headers, user_id=user_id, account_id=account_id, upload_id=upload.id)


@pytest.fixture
def alice(client: TestClient, db: Session, make_auth_headers: AuthHeadersFactory) -> Owner:
    return make_owner(client, db, make_auth_headers, "alice@example.com")


@pytest.fixture
def bob(client: TestClient, db: Session, make_auth_headers: AuthHeadersFactory) -> Owner:
    return make_owner(client, db, make_auth_headers, "bob@example.com")


@pytest.fixture
def system_categories(db: Session) -> dict[str, Category]:
    """The seeded system categories, by slug (the `db` fixture truncates)."""
    run_seed(db)
    return {category.slug: category for category in db.scalars(select(Category))}


def add_transaction(
    db: Session,
    owner: Owner,
    *,
    txn_date: datetime.date,
    amount_paise: int,
    direction: Direction = Direction.DEBIT,
    description: str = "UPI/SHOP/PAY",
    category_id: uuid.UUID | None = None,
    is_self_transfer: bool = False,
) -> Transaction:
    """Insert one transaction row for this owner and return it.

    The fingerprint only has to be unique per account here (the real one is
    a sha256 of the row's fields, see ADR 006), so a random 64-character
    hex string keeps every inserted row distinct.
    """
    transaction = Transaction(
        user_id=owner.user_id,
        account_id=owner.account_id,
        upload_id=owner.upload_id,
        txn_date=txn_date,
        amount_paise=amount_paise,
        direction=direction,
        raw_description=description,
        normalized_description=description.upper(),
        category_id=category_id,
        is_self_transfer=is_self_transfer,
        fingerprint=uuid.uuid4().hex * 2,
    )
    db.add(transaction)
    db.commit()
    return transaction


def get_summary(client: TestClient, owner: Owner, **params: str) -> dict[str, Any]:
    response = client.get(SUMMARY, params=params, headers=owner.headers)
    assert response.status_code == 200, response.text
    return response.json()


def get_breakdown(client: TestClient, owner: Owner, **params: str) -> dict[str, Any]:
    response = client.get(CATEGORIES, params=params, headers=owner.headers)
    assert response.status_code == 200, response.text
    return response.json()


# --- The sample month -------------------------------------------------------
#
# One row per line: (day, amount in paise, direction, is_self_transfer).
#
# August 2026, worked out by hand:
#
#   spend  =    7_000 (chai)
#          +  125_050 (zomato)
#          +   50_000 (ATM)
#          =  182_050            = Rs 1,820.50
#   income = 5_000_000 (salary)  = Rs 50,000.00
#   net    = 5_000_000 - 182_050 = 4_817_950 = Rs 48,179.50
#   count  = 4
#
# The two 20 August rows are a credit-card payment: Rs 10,000 leaves the
# savings account and the same Rs 10,000 arrives on the card. Both are
# flagged `is_self_transfer`, so neither is counted - that is SPEC §9.1.
# Counting them would add Rs 10,000 to BOTH income and spending in one
# month in which no money entered or left.

AUGUST = datetime.date(2026, 8, 1)
JULY = datetime.date(2026, 7, 1)

AUGUST_ROWS: tuple[tuple[int, int, Direction, bool], ...] = (
    (1, 7_000, Direction.DEBIT, False),  # chai, Rs 70
    (2, 125_050, Direction.DEBIT, False),  # zomato, Rs 1,250.50
    (3, 5_000_000, Direction.CREDIT, False),  # salary, Rs 50,000
    (15, 50_000, Direction.DEBIT, False),  # ATM, Rs 500
    (20, 1_000_000, Direction.DEBIT, True),  # card payment out
    (20, 1_000_000, Direction.CREDIT, True),  # ... and in
)

AUGUST_SPEND = 182_050
AUGUST_INCOME = 5_000_000
AUGUST_NET = 4_817_950
AUGUST_COUNT = 4

# July 2026, the month the summary compares against:
#
#   spend  =   100_000  = Rs 1,000
#   income = 4_000_000  = Rs 40,000
#   count  = 2
#
# so the changes August reports are
#
#   income: (5_000_000 - 4_000_000) / 4_000_000 = +25.0%
#   spend : (  182_050 -   100_000) /   100_000 = +82.05% -> 82.1 (half up)

JULY_ROWS: tuple[tuple[int, int, Direction, bool], ...] = (
    (3, 4_000_000, Direction.CREDIT, False),
    (10, 100_000, Direction.DEBIT, False),
)

JULY_SPEND = 100_000
JULY_INCOME = 4_000_000


def add_rows(
    db: Session,
    owner: Owner,
    month: datetime.date,
    rows: tuple[tuple[int, int, Direction, bool], ...],
) -> None:
    for day, amount_paise, direction, is_self_transfer in rows:
        add_transaction(
            db,
            owner,
            txn_date=month.replace(day=day),
            amount_paise=amount_paise,
            direction=direction,
            is_self_transfer=is_self_transfer,
        )


@pytest.fixture
def august(db: Session, alice: Owner) -> Owner:
    """Alice's August 2026 (the sample month above), and nothing else."""
    add_rows(db, alice, AUGUST, AUGUST_ROWS)
    return alice


@pytest.fixture
def august_and_july(db: Session, august: Owner) -> Owner:
    """Alice's August 2026 with July 2026 behind it, for the comparison."""
    add_rows(db, august, JULY, JULY_ROWS)
    return august


# --- Auth -------------------------------------------------------------------


@pytest.mark.parametrize("path", [SUMMARY, CATEGORIES])
def test_every_route_needs_a_token(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


# --- Summary: the totals ----------------------------------------------------


def test_the_monthly_totals_are_what_the_month_adds_up_to(
    client: TestClient, august: Owner
) -> None:
    body = get_summary(client, august, month="2026-08")

    assert body["month"] == "2026-08"
    assert body["from"] == "2026-08-01"
    assert body["to"] == "2026-08-31"
    assert body["current"]["income_paise"] == AUGUST_INCOME
    assert body["current"]["spend_paise"] == AUGUST_SPEND
    assert body["current"]["net_paise"] == AUGUST_NET
    assert body["current"]["transaction_count"] == AUGUST_COUNT


def test_the_totals_are_also_formatted_for_indian_readers(
    client: TestClient, august: Owner
) -> None:
    """Rule 10: lakh grouping, and the paise are never dropped."""
    current = get_summary(client, august, month="2026-08")["current"]

    assert current["income_display"] == "₹50,000.00"
    assert current["spend_display"] == "₹1,820.50"
    assert current["net_display"] == "₹48,179.50"


def test_a_month_where_more_went_out_than_came_in_has_a_negative_net(
    client: TestClient, db: Session, alice: Owner
) -> None:
    add_transaction(db, alice, txn_date=AUGUST, amount_paise=300_000, direction=Direction.DEBIT)
    add_transaction(db, alice, txn_date=AUGUST, amount_paise=50_000, direction=Direction.CREDIT)

    current = get_summary(client, alice, month="2026-08")["current"]

    assert current["net_paise"] == -250_000
    assert current["net_display"] == "-₹2,500.00"


def test_a_self_transfer_is_not_income_and_not_spending(
    client: TestClient, db: Session, alice: Owner
) -> None:
    """SPEC §9.1, proved in isolation: two flagged rows, all totals zero."""
    add_transaction(
        db,
        alice,
        txn_date=AUGUST,
        amount_paise=1_000_000,
        direction=Direction.DEBIT,
        is_self_transfer=True,
    )
    add_transaction(
        db,
        alice,
        txn_date=AUGUST,
        amount_paise=1_000_000,
        direction=Direction.CREDIT,
        is_self_transfer=True,
    )

    current = get_summary(client, alice, month="2026-08")["current"]

    assert current["income_paise"] == 0
    assert current["spend_paise"] == 0
    assert current["net_paise"] == 0
    # The count describes the rows behind the totals, so it excludes them too.
    assert current["transaction_count"] == 0


def test_only_the_requested_month_is_counted(client: TestClient, august_and_july: Owner) -> None:
    """The month boundaries are inclusive on both ends and exclude the rest."""
    body = get_summary(client, august_and_july, month="2026-08")

    assert body["current"]["spend_paise"] == AUGUST_SPEND
    assert body["previous"]["spend_paise"] == JULY_SPEND


def test_a_transaction_on_the_last_day_of_the_month_is_included(
    client: TestClient, db: Session, alice: Owner
) -> None:
    """31 August must be in August: an exclusive upper bound would lose it."""
    add_transaction(db, alice, txn_date=datetime.date(2026, 8, 31), amount_paise=12_345)

    assert get_summary(client, alice, month="2026-08")["current"]["spend_paise"] == 12_345


def test_a_month_with_no_transactions_is_zeros_not_an_error(
    client: TestClient, alice: Owner
) -> None:
    body = get_summary(client, alice, month="2026-08")

    assert body["current"] == {
        "month": "2026-08",
        "income_paise": 0,
        "spend_paise": 0,
        "net_paise": 0,
        "transaction_count": 0,
        "income_display": "₹0.00",
        "spend_display": "₹0.00",
        "net_display": "₹0.00",
    }


# --- Summary: the comparison ------------------------------------------------


def test_the_previous_month_is_reported_next_to_this_one(
    client: TestClient, august_and_july: Owner
) -> None:
    body = get_summary(client, august_and_july, month="2026-08")

    assert body["previous"]["month"] == "2026-07"
    assert body["previous"]["income_paise"] == JULY_INCOME
    assert body["previous"]["spend_paise"] == JULY_SPEND
    assert body["previous"]["net_paise"] == JULY_INCOME - JULY_SPEND
    assert body["previous"]["transaction_count"] == 2


def test_the_percent_changes_are_computed_against_the_previous_month(
    client: TestClient, august_and_july: Owner
) -> None:
    """The hand-computed figures from the comment above JULY_ROWS."""
    body = get_summary(client, august_and_july, month="2026-08")

    assert body["income_change_pct"] == 25.0
    # 82.05% rounded half up to one decimal place.
    assert body["spend_change_pct"] == 82.1


def test_a_percent_change_is_a_json_number_not_a_string(
    client: TestClient, august_and_july: Owner
) -> None:
    """A quoted "25.0" would have to be parsed before it could be drawn."""
    body = get_summary(client, august_and_july, month="2026-08")

    assert isinstance(body["income_change_pct"], float)


def test_percent_change_is_null_when_last_month_was_empty(
    client: TestClient, august: Owner
) -> None:
    """ "Safe when the previous month is 0" - and `null`, not 0 (see ADR 010)."""
    body = get_summary(client, august, month="2026-08")

    assert body["previous"]["income_paise"] == 0
    assert body["income_change_pct"] is None
    assert body["spend_change_pct"] is None


def test_the_comparison_month_crosses_the_year_boundary(
    client: TestClient, db: Session, alice: Owner
) -> None:
    """January's previous month is December of the year before."""
    add_transaction(db, alice, txn_date=datetime.date(2025, 12, 10), amount_paise=200_000)
    add_transaction(db, alice, txn_date=datetime.date(2026, 1, 10), amount_paise=300_000)

    body = get_summary(client, alice, month="2026-01")

    assert body["previous"]["month"] == "2025-12"
    assert body["previous"]["spend_paise"] == 200_000
    assert body["current"]["spend_paise"] == 300_000
    assert body["spend_change_pct"] == 50.0


def test_months_further_back_are_not_mixed_into_the_comparison(
    client: TestClient, db: Session, alice: Owner
) -> None:
    """Only the month before: June must not leak into July's totals."""
    add_transaction(db, alice, txn_date=datetime.date(2026, 6, 10), amount_paise=900_000)
    add_transaction(db, alice, txn_date=JULY.replace(day=10), amount_paise=100_000)
    add_transaction(db, alice, txn_date=AUGUST.replace(day=10), amount_paise=100_000)

    body = get_summary(client, alice, month="2026-08")

    assert body["previous"]["spend_paise"] == 100_000
    assert body["spend_change_pct"] == 0.0


# --- Summary: which month -------------------------------------------------


def test_no_month_means_the_current_month(client: TestClient, db: Session, alice: Owner) -> None:
    """Rule 10: "current" is the current month in India, not in UTC."""
    this_month = Month.current()
    add_transaction(db, alice, txn_date=this_month.first_day, amount_paise=4_200)

    body = get_summary(client, alice)

    assert body["month"] == this_month.label
    assert body["from"] == this_month.first_day.isoformat()
    assert body["to"] == this_month.last_day.isoformat()
    assert body["current"]["spend_paise"] == 4_200


def test_a_blank_month_means_the_current_month(client: TestClient, alice: Owner) -> None:
    assert get_summary(client, alice, month="")["month"] == Month.current().label


@pytest.mark.parametrize("month", ["2026-13", "2026-00", "2026-8", "last-august", "2026-08-01"])
def test_a_malformed_month_is_a_validation_error(
    client: TestClient, alice: Owner, month: str
) -> None:
    response = client.get(SUMMARY, params={"month": month}, headers=alice.headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_an_unknown_parameter_is_a_validation_error(client: TestClient, alice: Owner) -> None:
    """`?months=12` belongs to /insights/trend (Phase 2), not here."""
    response = client.get(SUMMARY, params={"months": "12"}, headers=alice.headers)

    assert response.status_code == 422


# --- Summary: rule 2 --------------------------------------------------------


def test_another_users_transactions_are_not_in_my_summary(
    client: TestClient, db: Session, alice: Owner, bob: Owner
) -> None:
    add_transaction(db, alice, txn_date=AUGUST, amount_paise=10_000)
    add_transaction(db, bob, txn_date=AUGUST, amount_paise=99_900_000)
    add_transaction(db, bob, txn_date=AUGUST, amount_paise=500_000, direction=Direction.CREDIT)

    body = get_summary(client, alice, month="2026-08")

    assert body["current"]["spend_paise"] == 10_000
    assert body["current"]["income_paise"] == 0
    assert body["current"]["transaction_count"] == 1


# --- Categories: the breakdown ---------------------------------------------


@pytest.fixture
def categorized_august(db: Session, august: Owner, system_categories: dict[str, Category]) -> Owner:
    """August with two of its three debits filed under Food & Dining.

    Worked out by hand:

        food-dining  =   7_000 + 125_050 = 132_050  (2 rows)
        uncategorized=                      50_000  (1 row, the ATM)
        total        =                     182_050  = AUGUST_SPEND

        shares: 132_050 / 182_050 = 72.535...% -> 72.5
                 50_000 / 182_050 = 27.465...% -> 27.5
    """
    food = system_categories["food-dining"].id
    rows = db.scalars(
        select(Transaction)
        .where(Transaction.amount_paise.in_([7_000, 125_050]))
        .order_by(Transaction.amount_paise)
    ).all()
    assert len(rows) == 2
    for row in rows:
        row.category_id = food
    db.commit()
    return august


FOOD_SPEND = 132_050
ATM_SPEND = 50_000


def test_spending_is_grouped_by_category_biggest_first(
    client: TestClient, categorized_august: Owner, system_categories: dict[str, Category]
) -> None:
    body = get_breakdown(client, categorized_august, **{"from": "2026-08-01", "to": "2026-08-31"})

    assert body["from"] == "2026-08-01"
    assert body["to"] == "2026-08-31"
    assert body["total_spend_paise"] == AUGUST_SPEND
    assert body["total_spend_display"] == "₹1,820.50"

    food, uncategorized = body["items"]
    assert food["category_id"] == str(system_categories["food-dining"].id)
    assert food["name"] == "Food & Dining"
    assert food["slug"] == "food-dining"
    assert food["kind"] == "expense"
    assert food["icon"] == "utensils"
    assert food["color"] == "#F97316"
    assert food["amount_paise"] == FOOD_SPEND
    assert food["amount_display"] == "₹1,320.50"
    assert food["transaction_count"] == 2
    assert food["share_pct"] == 72.5

    assert uncategorized["amount_paise"] == ATM_SPEND
    assert uncategorized["share_pct"] == 27.5


def test_uncategorized_spending_is_its_own_bucket(
    client: TestClient, categorized_august: Owner
) -> None:
    """Kept rather than dropped, so the parts add up (see ADR 010)."""
    items = get_breakdown(client, categorized_august, **{"from": "2026-08-01", "to": "2026-08-31"})[
        "items"
    ]
    bucket = next(item for item in items if item["category_id"] is None)

    assert bucket["name"] == "Uncategorized"
    assert bucket["slug"] is None
    assert bucket["kind"] is None
    assert bucket["icon"] is None
    assert bucket["color"] is None
    assert bucket["amount_paise"] == ATM_SPEND
    assert bucket["transaction_count"] == 1


def test_the_parts_add_up_to_the_total(client: TestClient, categorized_august: Owner) -> None:
    body = get_breakdown(client, categorized_august, **{"from": "2026-08-01", "to": "2026-08-31"})

    assert sum(item["amount_paise"] for item in body["items"]) == body["total_spend_paise"]


def test_income_never_appears_in_the_breakdown(
    client: TestClient, db: Session, alice: Owner, system_categories: dict[str, Category]
) -> None:
    """A salary credit filed under "Salary" is income, not spending."""
    add_transaction(
        db,
        alice,
        txn_date=AUGUST,
        amount_paise=5_000_000,
        direction=Direction.CREDIT,
        category_id=system_categories["salary"].id,
    )
    add_transaction(db, alice, txn_date=AUGUST, amount_paise=1_000)

    body = get_breakdown(client, alice, **{"from": "2026-08-01", "to": "2026-08-31"})

    assert body["total_spend_paise"] == 1_000
    assert [item["slug"] for item in body["items"]] == [None]


def test_a_self_transfer_never_appears_in_the_breakdown(
    client: TestClient, db: Session, alice: Owner, system_categories: dict[str, Category]
) -> None:
    add_transaction(
        db,
        alice,
        txn_date=AUGUST,
        amount_paise=1_000_000,
        category_id=system_categories["self-transfer"].id,
        is_self_transfer=True,
    )
    add_transaction(db, alice, txn_date=AUGUST, amount_paise=1_000)

    body = get_breakdown(client, alice, **{"from": "2026-08-01", "to": "2026-08-31"})

    assert body["total_spend_paise"] == 1_000
    assert len(body["items"]) == 1


def test_a_users_own_category_appears_with_its_own_name(
    client: TestClient, db: Session, alice: Owner
) -> None:
    """The LEFT JOIN resolves user categories too, not only system ones."""
    from app.models import CategoryKind

    category = Category(
        user_id=alice.user_id,
        name="Chai fund",
        slug="chai-fund",
        kind=CategoryKind.EXPENSE,
        icon="coffee",
        color="#111111",
    )
    db.add(category)
    db.commit()
    add_transaction(db, alice, txn_date=AUGUST, amount_paise=9_900, category_id=category.id)

    items = get_breakdown(client, alice, **{"from": "2026-08-01", "to": "2026-08-31"})["items"]

    assert items[0]["name"] == "Chai fund"
    assert items[0]["slug"] == "chai-fund"
    assert items[0]["share_pct"] == 100.0


def test_a_range_with_no_spending_is_an_empty_breakdown(client: TestClient, august: Owner) -> None:
    body = get_breakdown(client, august, **{"from": "2026-09-01", "to": "2026-09-30"})

    assert body["items"] == []
    assert body["total_spend_paise"] == 0
    assert body["total_spend_display"] == "₹0.00"


def test_both_ends_of_the_range_are_inclusive(client: TestClient, august: Owner) -> None:
    """1 and 2 August only: Rs 70 + Rs 1,250.50."""
    body = get_breakdown(client, august, **{"from": "2026-08-01", "to": "2026-08-02"})

    assert body["total_spend_paise"] == 7_000 + 125_050


def test_a_single_day_range_is_allowed(client: TestClient, august: Owner) -> None:
    body = get_breakdown(client, august, **{"from": "2026-08-15", "to": "2026-08-15"})

    assert body["total_spend_paise"] == ATM_SPEND


def test_no_dates_mean_the_current_month(client: TestClient, db: Session, alice: Owner) -> None:
    this_month = Month.current()
    add_transaction(db, alice, txn_date=this_month.first_day, amount_paise=3_300)

    body = get_breakdown(client, alice)

    assert body["from"] == this_month.first_day.isoformat()
    assert body["to"] == this_month.last_day.isoformat()
    assert body["total_spend_paise"] == 3_300


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-08-01"},  # half a range
        {"to": "2026-08-31"},  # the other half
        {"from": "2026-09-01", "to": "2026-08-01"},  # backwards
        {"from": "not-a-date", "to": "2026-08-31"},
        {"month": "2026-08"},  # the summary's parameter, not this one's
    ],
)
def test_a_bad_range_is_a_validation_error(
    client: TestClient, alice: Owner, params: dict[str, str]
) -> None:
    response = client.get(CATEGORIES, params=params, headers=alice.headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_another_users_spending_is_not_in_my_breakdown(
    client: TestClient,
    db: Session,
    alice: Owner,
    bob: Owner,
    system_categories: dict[str, Category],
) -> None:
    food = system_categories["food-dining"].id
    add_transaction(db, alice, txn_date=AUGUST, amount_paise=10_000, category_id=food)
    add_transaction(db, bob, txn_date=AUGUST, amount_paise=99_900_000, category_id=food)

    body = get_breakdown(client, alice, **{"from": "2026-08-01", "to": "2026-08-31"})

    assert body["total_spend_paise"] == 10_000
    assert body["items"][0]["transaction_count"] == 1


# --- The link between manual categorization and the totals ------------------


def test_filing_a_row_as_a_self_transfer_removes_it_from_the_summary(
    client: TestClient, db: Session, alice: Owner, system_categories: dict[str, Category]
) -> None:
    """The whole point of the Step 8 decision, end to end.

    A credit-card payment arrives as an ordinary debit, so it counts as
    spending. The user files it under "Self Transfer"; `PATCH` sets
    `is_self_transfer` from the category's kind (`transfer`), and the
    summary stops counting it. Without that link the user would see the
    right category and the wrong total, with no way to fix it.
    """
    transaction = add_transaction(db, alice, txn_date=AUGUST, amount_paise=1_000_000)
    add_transaction(db, alice, txn_date=AUGUST, amount_paise=25_000)

    before = get_summary(client, alice, month="2026-08")["current"]
    assert before["spend_paise"] == 1_025_000

    response = client.patch(
        f"{TRANSACTIONS}/{transaction.id}",
        json={"category_id": str(system_categories["self-transfer"].id)},
        headers=alice.headers,
    )
    assert response.status_code == 200
    assert response.json()["is_self_transfer"] is True

    after = get_summary(client, alice, month="2026-08")["current"]
    assert after["spend_paise"] == 25_000
    assert after["transaction_count"] == 1


def test_categorizing_a_row_moves_it_in_the_breakdown(
    client: TestClient, db: Session, alice: Owner, system_categories: dict[str, Category]
) -> None:
    """Uncategorized -> Groceries, with the total unchanged."""
    transaction = add_transaction(db, alice, txn_date=AUGUST, amount_paise=65_000)
    august_range = {"from": "2026-08-01", "to": "2026-08-31"}

    before = get_breakdown(client, alice, **august_range)["items"]
    assert [item["category_id"] for item in before] == [None]

    response = client.patch(
        f"{TRANSACTIONS}/{transaction.id}",
        json={"category_id": str(system_categories["groceries"].id)},
        headers=alice.headers,
    )
    assert response.status_code == 200

    body = get_breakdown(client, alice, **august_range)
    assert [item["slug"] for item in body["items"]] == ["groceries"]
    assert body["total_spend_paise"] == 65_000
