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

from app.categorizer.base import CategorySource, Decision
from app.db.seed import run_seed
from app.models import Category, Merchant, StatementUpload, Transaction
from app.services import categorization as categorization_service
from app.services.categorization import categorize_upload
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
    assert imported["rows_categorized"] == 6


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
    warning = next(entry for entry in logs if entry["event"] == "unknown_category_slug")
    assert warning["category_slug"] == "nope"


def test_a_rule_decision_uses_the_rules_category_id_as_is() -> None:
    """A user rule names its category by id, not by system slug.

    Called directly because no rules can exist before Step 7 builds their
    table; Step 7's end-to-end tests then cover this path through an upload.
    """
    own_category = uuid.uuid4()
    decision = Decision(CategorySource.USER_RULE, Decimal("1.00"), category_id=own_category)

    # Even with no system categories loaded at all.
    assert categorization_service._category_id(decision, {}) == own_category


def test_without_seeded_categories_nothing_is_categorized(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    """An empty database (no seed) imports fine, just uncategorized."""
    body = import_rows(client, alice, tmp_path)

    assert all(row.category_id is None for row in transactions_of(db, body["id"]))
