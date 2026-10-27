"""Uploads are categorized on import (Phase 2 Step 6, SPEC §8, ADR 016).

End to end: a statement goes through POST /uploads and the (synchronous
test) worker, and the stored rows come back categorized. The handlers'
logic is covered in tests/unit/test_categorizer.py; this file checks what
only the database can show - category ids, merchant links, and that a
user's manual choice survives.

Narrations use the real ICICI layouts with invented names, VPAs and
reference numbers (CLAUDE.md rule 8).
"""

import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.categorizer.base import CategorySource
from app.categorizer.handlers import RuleMatchType
from app.db.seed import run_seed
from app.models import (
    Category,
    CategoryKind,
    CategoryRule,
    Merchant,
    StatementUpload,
    Transaction,
    User,
)
from app.services.categorization import categorize_upload, recategorize_user
from tests.integration.test_uploads import (
    AuthHeadersFactory,
    import_file,
    make_account,
    statement_bytes,
    transactions_of,
)

pytestmark = pytest.mark.integration

TXN = "ICI4f2a9c1e7b3d5f6a8c0e2b4d6f8a1c3e"


def upi(name: str, vpa: str, remark: str, bank: str, rrn: str) -> str:
    return f"UPI/{name}/{vpa}/{remark}/{bank}/{rrn}/{TXN}"


# (S No, value date, txn date, cheque, remarks, withdrawal, deposit, balance)
ROWS = (
    # A local shop's QR code: no rule can know its category
    ("1", "01/08/2026", "01/08/2026", "",
     upi("MOON CAFE", "Q123456789@ybl", "NO REMARK", "YES BANK L", "512345678901"),
     "120.00", "0.00", "99880.00"),
    # Swiggy, cut to 10 characters by the bank: found through dictionary_key
    ("2", "02/08/2026", "02/08/2026", "",
     upi("Swiggy Lim", "swiggypayu@hdf", "NO REMARK", "HDFC BANK", "512345678902"),
     "450.00", "0.00", "99430.00"),
    # A person, paid
    ("3", "03/08/2026", "03/08/2026", "",
     upi("ASHA VERMA", "9876543210@upi", "Sent using", "STATE BANK", "512345678903"),
     "500.00", "0.00", "98930.00"),
    # A person, paying you
    ("4", "04/08/2026", "04/08/2026", "",
     upi("RAVI KUMAR", "ravi.kumar@ok", "UPI", "AXIS BANK", "512345678904"),
     "0.00", "1000.00", "99930.00"),
    # Cash from another bank's ATM
    ("5", "05/08/2026", "05/08/2026", "",
     "NFS/CASH WDL/612345678913/12345678/MUMBAI CEN/05-08-261430",
     "2000.00", "0.00", "97930.00"),
    # BHIM cashback
    ("6", "06/08/2026", "06/08/2026", "",
     upi("NPCI BHIM", "npcibhimcash@h", "BHIMCASHBA", "HDFC BANK", "512345678906"),
     "0.00", "25.00", "97955.00"),
    # Money back from a known merchant: a refund
    ("7", "07/08/2026", "07/08/2026", "",
     upi("Zomato Ltd", "zomatoorder@yb", "NO REMARK", "YES BANK L", "512345678907"),
     "0.00", "300.00", "98255.00"),
    # A person paid with the remark "charges": not a bank fee (review)
    ("8", "08/08/2026", "08/08/2026", "",
     upi("RAMESH KUM", "9876543211@upi", "charges", "YES BANK L", "512345678908"),
     "200.00", "0.00", "98055.00"),
    # A person paid with the remark "hotel": still a person (review)
    ("9", "09/08/2026", "09/08/2026", "",
     upi("NEHA PATEL", "9876543212@upi", "hotel", "STATE BANK", "512345678909"),
     "150.00", "0.00", "97905.00"),
)  # fmt: skip


@pytest.fixture
def seeded(db: Session) -> None:
    """The system categories and the merchant dictionary (the `db` fixture truncates)."""
    run_seed(db)


@pytest.fixture
def alice(make_auth_headers: AuthHeadersFactory) -> dict[str, str]:
    return make_auth_headers("alice@example.com")


def import_rows(client: TestClient, headers: dict[str, str], tmp_path: Path) -> dict[str, Any]:
    account_id = make_account(client, headers)
    content = statement_bytes(tmp_path, rows=ROWS)
    body = import_file(client, headers, content=content, account_id=account_id)
    assert body["status"] == "completed"
    return body


def slug_of(db: Session, category_id: uuid.UUID | None) -> str | None:
    if category_id is None:
        return None
    return db.scalar(select(Category.slug).where(Category.id == category_id))


def merchant_key_of(db: Session, merchant_id: uuid.UUID | None) -> str | None:
    if merchant_id is None:
        return None
    return db.scalar(select(Merchant.normalized_key).where(Merchant.id == merchant_id))


@pytest.mark.usefixtures("seeded")
def test_an_upload_is_categorized_on_import(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    body = import_rows(client, alice, tmp_path)
    rows = transactions_of(db, body["id"])

    got = [
        (
            slug_of(db, row.category_id),
            row.category_source,
            row.category_confidence,
            merchant_key_of(db, row.merchant_id),
        )
        for row in rows
    ]
    assert got == [
        (None, CategorySource.NONE, None, None),
        ("food-dining", CategorySource.MERCHANT, Decimal("0.90"), "SWIGGY"),
        ("transfers-to-people", CategorySource.HEURISTIC, Decimal("0.70"), None),
        ("other-income", CategorySource.HEURISTIC, Decimal("0.70"), None),
        ("cash-withdrawal", CategorySource.KEYWORD, Decimal("0.90"), None),
        ("refunds", CategorySource.KEYWORD, Decimal("0.90"), None),
        ("refunds", CategorySource.MERCHANT, Decimal("0.90"), "ZOMATO"),
        ("transfers-to-people", CategorySource.HEURISTIC, Decimal("0.70"), None),
        ("transfers-to-people", CategorySource.HEURISTIC, Decimal("0.70"), None),
    ]
    # The categorizer never decides a self transfer (that is Step 8).
    assert not any(row.is_self_transfer for row in rows)


@pytest.mark.usefixtures("seeded")
def test_a_manual_category_survives_another_run(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    body = import_rows(client, alice, tmp_path)
    person = transactions_of(db, body["id"])[2]  # categorized as a person by the heuristic
    groceries = db.scalar(
        select(Category.id).where(Category.slug == "groceries", Category.user_id.is_(None))
    )
    response = client.patch(
        f"/api/v1/transactions/{person.id}", json={"category_id": str(groceries)}, headers=alice
    )
    assert response.status_code == 200

    db.expire_all()
    upload = db.get_one(StatementUpload, uuid.UUID(body["id"]))
    # Nothing is left for it to do: the shop still matches nothing, and the
    # user's choice is not looked at again.
    assert categorize_upload(db, upload) == 0
    db.commit()

    db.expire_all()
    row = db.get_one(Transaction, person.id)
    assert row.category_id == groceries
    assert row.category_source is CategorySource.USER_MANUAL


@pytest.mark.usefixtures("seeded")
def test_the_import_log_counts_categorized_rows(
    client: TestClient, alice: dict[str, str], tmp_path: Path
) -> None:
    with structlog.testing.capture_logs() as logs:
        import_rows(client, alice, tmp_path)

    imported = next(entry for entry in logs if entry["event"] == "statement_imported")
    assert imported["rows_categorized"] == 8


@pytest.mark.usefixtures("seeded")
def test_a_merchant_pointing_at_an_unknown_category_leaves_the_row_alone(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    """Seed and code disagreeing is logged, not a failed import."""
    db.add(Merchant(normalized_key="MOONCAFE", display_name="Moon", default_category_slug="nope"))
    db.commit()

    with structlog.testing.capture_logs() as logs:
        body = import_rows(client, alice, tmp_path)

    moon = transactions_of(db, body["id"])[0]
    assert moon.category_id is None
    assert moon.category_source is CategorySource.NONE
    # Still linked to its merchant: the link does not depend on the category.
    assert merchant_key_of(db, moon.merchant_id) == "MOONCAFE"
    # ONE line per unknown slug, with the count - not one per row (review).
    warnings = [entry for entry in logs if entry["event"] == "unknown_category_slug"]
    assert [(entry["category_slug"], entry["rows_count"]) for entry in warnings] == [("nope", 1)]


# --- User-rule categories -----------------------------------------------------
# The rules are written straight into `category_rules` (Step 7's table), so
# a rule the API would refuse - another user's category - can be stored too,
# and the check where the id is WRITTEN is tested on its own.


def store_rule(db: Session, email: str, category_id: uuid.UUID) -> None:
    """A "MOON CAFE" rule for the user with this email."""
    user_id = db.scalar(select(User.id).where(User.email == email))
    assert user_id is not None
    db.add(
        CategoryRule(
            user_id=user_id,
            match_type=RuleMatchType.CONTAINS,
            pattern="MOON CAFE",
            category_id=category_id,
        )
    )
    db.commit()


def own_category(db: Session, email: str) -> uuid.UUID:
    """A category owned by the user with this email."""
    user_id = db.scalar(select(User.id).where(User.email == email))
    category = Category(user_id=user_id, name="Coffee", slug="coffee", kind=CategoryKind.EXPENSE)
    db.add(category)
    db.commit()
    return category.id


@pytest.mark.usefixtures("seeded")
def test_a_rule_may_file_a_row_under_the_users_own_category(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    coffee = own_category(db, "alice@example.com")
    store_rule(db, "alice@example.com", coffee)

    body = import_rows(client, alice, tmp_path)

    moon = transactions_of(db, body["id"])[0]
    assert moon.category_id == coffee
    assert moon.category_source is CategorySource.USER_RULE


@pytest.mark.usefixtures("seeded")
def test_a_rule_pointing_at_another_users_category_is_refused(
    client: TestClient,
    alice: dict[str, str],
    make_auth_headers: AuthHeadersFactory,
    tmp_path: Path,
    db: Session,
) -> None:
    """The ownership check where the id is WRITTEN (review of Step 6).

    The insights breakdown joins categories without a user filter (ADR 010),
    so a stored category_id must always be a system one or the user's own.
    `POST /rules` refuses such a rule (tests/integration/test_rules.py);
    this proves the import does not rely on that alone.
    """
    make_auth_headers("bob@example.com")
    bobs = own_category(db, "bob@example.com")
    store_rule(db, "alice@example.com", bobs)

    with structlog.testing.capture_logs() as logs:
        body = import_rows(client, alice, tmp_path)

    moon = transactions_of(db, body["id"])[0]
    assert moon.category_id is None
    assert moon.category_source is CategorySource.NONE
    refused = next(entry for entry in logs if entry["event"] == "category_not_usable_by_user")
    assert refused["rows_count"] == 1


@pytest.mark.usefixtures("seeded")
def test_a_rerun_refuses_another_users_category_too(
    client: TestClient,
    alice: dict[str, str],
    make_auth_headers: AuthHeadersFactory,
    tmp_path: Path,
    db: Session,
) -> None:
    """The same write-time check in `recategorize_user` (Step 7), logged
    without an upload id because a re-run spans all of the user's uploads."""
    make_auth_headers("bob@example.com")
    body = import_rows(client, alice, tmp_path)
    store_rule(db, "alice@example.com", own_category(db, "bob@example.com"))
    alice_id = db.scalar(select(User.id).where(User.email == "alice@example.com"))
    assert alice_id is not None

    with structlog.testing.capture_logs() as logs:
        recategorize_user(db, alice_id)

    db.expire_all()
    assert transactions_of(db, body["id"])[0].category_id is None
    refused = next(entry for entry in logs if entry["event"] == "category_not_usable_by_user")
    assert refused["rows_count"] == 1
    assert "upload_id" not in refused


def test_without_seeded_categories_nothing_is_categorized(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    """An empty database (no seed) imports fine, just uncategorized."""
    body = import_rows(client, alice, tmp_path)

    assert all(row.category_id is None for row in transactions_of(db, body["id"]))
