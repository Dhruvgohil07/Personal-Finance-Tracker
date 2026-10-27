"""Self-transfer detection end to end (Phase 2 Step 8, SPEC §8.4, ADR 018).

Statements are imported into two (sometimes three) of the user's accounts,
through POST /uploads and the synchronous test worker, so detection runs
exactly where it runs in production: at the end of the import's
categorization, and at the end of every rule re-run. The pairing logic
itself is covered in tests/unit/test_self_transfer.py.

Narrations use the real ICICI UPI layout with invented names, VPAs and
reference numbers (CLAUDE.md rule 8). Each statement's balances chain, so
every import is `completed`.
"""

import uuid
from pathlib import Path
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.categorizer.base import CategorySource
from app.db.seed import run_seed
from app.models import Category, Transaction, User
from app.services import categorization as categorization_service
from app.services import transactions as transactions_service
from app.services.categorization import SELF_TRANSFER_CONFIDENCE, recategorize_user
from tests.fixtures.icici_xls import build_icici_xls
from tests.integration.test_categorization import upi
from tests.integration.test_uploads import (
    AuthHeadersFactory,
    import_file,
    make_account,
    transactions_of,
)

pytestmark = pytest.mark.integration

# (S No, value date, txn date, cheque, remarks, withdrawal, deposit, balance)
SAVINGS_ROWS = (
    # 0: ₹15,000 sent to my own other account (the P2P heuristic's guess: a person)
    ("1", "03/08/2026", "03/08/2026", "",
     upi("DHRUV G", "9999999999@ybl", "self", "HDFC BANK", "512345678901"),
     "15000.00", "0.00", "85000.00"),
    # 1: a local shop: real spending
    ("2", "04/08/2026", "04/08/2026", "",
     upi("MOON CAFE", "Q123456789@ybl", "NO REMARK", "YES BANK L", "512345678902"),
     "120.00", "0.00", "84880.00"),
    # 2: a real person: real spending
    ("3", "05/08/2026", "05/08/2026", "",
     upi("ASHA VERMA", "9876543210@upi", "Sent using", "STATE BANK", "512345678903"),
     "500.00", "0.00", "84380.00"),
)  # fmt: skip
SENT, SHOP, PERSON = range(3)

# The other end of row 0, a day later, in account ...5678.
OTHER_ROWS = (
    ("1", "04/08/2026", "04/08/2026", "",
     upi("DHRUV G", "9999999998@ybl", "self", "ICICI BANK", "512345678904"),
     "0.00", "15000.00", "35000.00"),
)  # fmt: skip

# A third account receiving the SAME amount on the same day: now nobody can
# tell which of the two credits the ₹15,000 became.
THIRD_ROWS = (
    ("1", "04/08/2026", "04/08/2026", "",
     upi("DHRUV G", "9999999997@ybl", "self", "AXIS BANK", "512345678905"),
     "0.00", "15000.00", "65000.00"),
)  # fmt: skip


@pytest.fixture
def seeded(db: Session) -> None:
    run_seed(db)


@pytest.fixture
def alice(make_auth_headers: AuthHeadersFactory) -> dict[str, str]:
    return make_auth_headers("alice@example.com")


def import_into(
    client: TestClient,
    headers: dict[str, str],
    tmp_path: Path,
    rows: tuple[tuple[str, ...], ...],
    last4: str,
) -> str:
    """Add an account ending in `last4` and import `rows` into it."""
    account_id = make_account(client, headers, masked_number=last4)
    path = build_icici_xls(tmp_path / f"{last4}.xls", rows, account_number=f"08460000{last4}")
    body = import_file(client, headers, content=path.read_bytes(), account_id=account_id)
    assert body["status"] == "completed"
    upload_id: str = body["id"]
    return upload_id


def rows_of(db: Session, upload_id: str) -> list[Transaction]:
    db.expire_all()  # what the requests and jobs committed, not a cache
    return transactions_of(db, upload_id)


def state(db: Session, row: Transaction) -> tuple[str | None, CategorySource, bool]:
    """(category slug, source, is_self_transfer) of a row."""
    slug = None
    if row.category_id is not None:
        slug = db.scalar(select(Category.slug).where(Category.id == row.category_id))
    return slug, row.category_source, row.is_self_transfer


def system_id(db: Session, slug: str) -> str:
    return str(db.scalar(select(Category.id).where(Category.slug == slug)))


PAIRED = ("self-transfer", CategorySource.HEURISTIC, True)


def import_both(client: TestClient, headers: dict[str, str], tmp_path: Path) -> tuple[str, str]:
    savings = import_into(client, headers, tmp_path, SAVINGS_ROWS, "1234")
    other = import_into(client, headers, tmp_path, OTHER_ROWS, "5678")
    return savings, other


# --- Detection on import ------------------------------------------------------------------


@pytest.mark.usefixtures("seeded")
def test_a_transfer_between_two_accounts_is_paired(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    savings, other = import_both(client, alice, tmp_path)

    rows = rows_of(db, savings)
    [received] = rows_of(db, other)
    assert state(db, rows[SENT]) == PAIRED
    assert state(db, received) == PAIRED
    assert rows[SENT].category_confidence == SELF_TRANSFER_CONFIDENCE
    # Real spending is left exactly as the chain decided it.
    assert state(db, rows[SHOP]) == (None, CategorySource.NONE, False)
    assert state(db, rows[PERSON]) == ("transfers-to-people", CategorySource.HEURISTIC, False)


@pytest.mark.usefixtures("seeded")
def test_the_import_order_does_not_matter(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    """The credit imported FIRST finds its debit when the debit arrives."""
    other = import_into(client, alice, tmp_path, OTHER_ROWS, "5678")
    [received] = rows_of(db, other)
    assert state(db, received) == ("other-income", CategorySource.HEURISTIC, False)

    savings = import_into(client, alice, tmp_path, SAVINGS_ROWS, "1234")

    assert state(db, rows_of(db, savings)[SENT]) == PAIRED
    assert state(db, rows_of(db, other)[0]) == PAIRED


@pytest.mark.usefixtures("seeded")
def test_the_summary_leaves_the_pair_out(
    client: TestClient, alice: dict[str, str], tmp_path: Path
) -> None:
    import_both(client, alice, tmp_path)

    response = client.get("/api/v1/insights/summary", params={"month": "2026-08"}, headers=alice)

    current = response.json()["current"]
    # Spending is the shop and the person only; the ₹15,000 is neither
    # spending nor income.
    assert (current["spend_paise"], current["income_paise"]) == (62_000, 0)
    assert current["transaction_count"] == 2


@pytest.mark.usefixtures("seeded")
def test_a_new_import_that_makes_a_pair_ambiguous_unpairs_it(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    """Two credits of the same amount the same day: no longer a sure pair,
    so both former halves go back to what the chain says."""
    savings, other = import_both(client, alice, tmp_path)

    with structlog.testing.capture_logs() as logs:
        third = import_into(client, alice, tmp_path, THIRD_ROWS, "4321")

    assert state(db, rows_of(db, savings)[SENT])[2] is False
    assert state(db, rows_of(db, savings)[SENT])[:2] == (
        "transfers-to-people",
        CategorySource.HEURISTIC,
    )
    assert state(db, rows_of(db, other)[0]) == ("other-income", CategorySource.HEURISTIC, False)
    assert state(db, rows_of(db, third)[0])[2] is False
    detected = [entry for entry in logs if entry["event"] == "self_transfers_detected"]
    assert (detected[-1]["pairs_count"], detected[-1]["rows_unpaired"]) == (0, 2)


@pytest.mark.usefixtures("seeded")
def test_two_users_rows_are_never_paired(
    client: TestClient,
    alice: dict[str, str],
    make_auth_headers: AuthHeadersFactory,
    tmp_path: Path,
    db: Session,
) -> None:
    bob = make_auth_headers("bob@example.com")
    savings = import_into(client, alice, tmp_path, SAVINGS_ROWS, "1234")
    bobs = import_into(client, bob, tmp_path, OTHER_ROWS, "5678")

    assert state(db, rows_of(db, savings)[SENT])[2] is False
    assert state(db, rows_of(db, bobs)[0])[2] is False


def test_without_the_seed_a_pair_is_logged_not_written(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    """Seed and code disagreeing is logged once, like any unknown slug."""
    import_into(client, alice, tmp_path, SAVINGS_ROWS, "1234")

    with structlog.testing.capture_logs() as logs:
        other = import_into(client, alice, tmp_path, OTHER_ROWS, "5678")

    assert state(db, rows_of(db, other)[0]) == (None, CategorySource.NONE, False)
    # The chain's own slugs are unknown too without a seed; only ours here.
    unknown = [
        entry["rows_count"]
        for entry in logs
        if entry["event"] == "unknown_category_slug" and entry["category_slug"] == "self-transfer"
    ]
    assert unknown == [2]


# --- Detection survives a rule re-run (ADR 017) ---------------------------------------------


def alice_id(db: Session) -> uuid.UUID:
    user_id = db.scalar(select(User.id).where(User.email == "alice@example.com"))
    assert user_id is not None
    return user_id


@pytest.mark.usefixtures("seeded")
def test_a_rule_change_keeps_the_pair(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    savings, other = import_both(client, alice, tmp_path)

    response = client.post(
        "/api/v1/rules",
        json={
            "match_type": "merchant",
            "pattern": "moon cafe",
            "category_id": system_id(db, "food-dining"),
        },
        headers=alice,
    )
    assert response.status_code == 201

    assert state(db, rows_of(db, savings)[SENT]) == PAIRED
    assert state(db, rows_of(db, other)[0]) == PAIRED
    assert state(db, rows_of(db, savings)[SHOP])[0] == "food-dining"


@pytest.mark.usefixtures("seeded")
def test_a_rerun_of_a_paired_user_changes_nothing(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    import_both(client, alice, tmp_path)

    assert recategorize_user(db, alice_id(db)) == 0


@pytest.mark.usefixtures("seeded")
def test_a_user_rule_beats_detection(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    """You said so: a rule decides its rows, and their partner is left unpaired."""
    savings, other = import_both(client, alice, tmp_path)

    client.post(
        "/api/v1/rules",
        json={
            "match_type": "vpa",
            "pattern": "9999999999@ybl",
            "category_id": system_id(db, "rent"),
        },
        headers=alice,
    )

    assert state(db, rows_of(db, savings)[SENT]) == ("rent", CategorySource.USER_RULE, False)
    assert state(db, rows_of(db, other)[0]) == ("other-income", CategorySource.HEURISTIC, False)


# --- Re-filing one half by hand (decided with Dhruv) ----------------------------------------


@pytest.mark.usefixtures("seeded")
def test_refiling_one_half_by_hand_unpairs_the_other(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    savings, other = import_both(client, alice, tmp_path)
    sent = rows_of(db, savings)[SENT]

    response = client.patch(
        f"/api/v1/transactions/{sent.id}",
        json={"category_id": system_id(db, "rent")},
        headers=alice,
    )

    assert response.status_code == 200
    assert state(db, rows_of(db, savings)[SENT]) == ("rent", CategorySource.USER_MANUAL, False)
    # Re-decided at once by the re-run, not left out of the totals.
    assert state(db, rows_of(db, other)[0]) == ("other-income", CategorySource.HEURISTIC, False)


@pytest.mark.usefixtures("seeded")
def test_a_note_on_a_paired_row_does_not_rerun(
    client: TestClient,
    alice: dict[str, str],
    tmp_path: Path,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    savings, _ = import_both(client, alice, tmp_path)
    sent = rows_of(db, savings)[SENT]
    reruns: list[Any] = []
    monkeypatch.setattr(
        categorization_service, "commit_and_recategorize", lambda *args: reruns.append(args)
    )

    response = client.patch(
        f"/api/v1/transactions/{sent.id}", json={"notes": "to my HDFC"}, headers=alice
    )

    assert response.status_code == 200
    assert reruns == []
    assert state(db, rows_of(db, savings)[SENT]) == PAIRED


@pytest.mark.usefixtures("seeded")
def test_refiling_an_unpaired_row_does_not_rerun(
    client: TestClient,
    alice: dict[str, str],
    tmp_path: Path,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    savings, _ = import_both(client, alice, tmp_path)
    shop = rows_of(db, savings)[SHOP]
    reruns: list[Any] = []
    monkeypatch.setattr(
        categorization_service, "commit_and_recategorize", lambda *args: reruns.append(args)
    )

    client.patch(
        f"/api/v1/transactions/{shop.id}",
        json={"category_id": system_id(db, "food-dining")},
        headers=alice,
    )

    assert reruns == []


# --- Lock order (review of Step 8) ---------------------------------------------------------


@pytest.mark.usefixtures("seeded")
def test_a_category_change_locks_the_user_before_touching_the_row(
    client: TestClient,
    alice: dict[str, str],
    tmp_path: Path,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock first, rows after - the order every run uses. The other way
    round, an inline re-run (Redis down) could deadlock with a running job."""
    savings, _ = import_both(client, alice, tmp_path)
    sent = rows_of(db, savings)[SENT]
    calls: list[str] = []
    real_lock = categorization_service.lock_categorization
    real_set = transactions_service._set_category

    def lock(*args: Any) -> None:
        calls.append("lock")
        real_lock(*args)

    def set_category(*args: Any) -> None:
        calls.append("set_category")
        real_set(*args)

    monkeypatch.setattr(categorization_service, "lock_categorization", lock)
    monkeypatch.setattr(transactions_service, "_set_category", set_category)

    client.patch(
        f"/api/v1/transactions/{sent.id}",
        json={"category_id": system_id(db, "rent")},
        headers=alice,
    )

    # The re-run that follows takes the lock again (re-entrant in Postgres).
    assert calls[:2] == ["lock", "set_category"]


# --- Deleting one half (review of Step 8) --------------------------------------------------


@pytest.mark.usefixtures("seeded")
def test_deleting_one_halfs_upload_re_decides_the_other(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    savings, other = import_both(client, alice, tmp_path)

    assert client.delete(f"/api/v1/uploads/{other}", headers=alice).status_code == 204

    # Back in the totals as what the chain says, not a transfer with no partner.
    assert state(db, rows_of(db, savings)[SENT]) == (
        "transfers-to-people",
        CategorySource.HEURISTIC,
        False,
    )


@pytest.mark.usefixtures("seeded")
def test_deleting_one_halfs_account_re_decides_the_other(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    savings, other = import_both(client, alice, tmp_path)
    [received] = rows_of(db, other)

    response = client.delete(f"/api/v1/accounts/{received.account_id}", headers=alice)

    assert response.status_code == 204
    assert state(db, rows_of(db, savings)[SENT])[2] is False


@pytest.mark.usefixtures("seeded")
def test_a_delete_that_breaks_no_pair_does_not_rerun(
    client: TestClient,
    alice: dict[str, str],
    tmp_path: Path,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload = import_into(client, alice, tmp_path, SAVINGS_ROWS, "1234")
    [row, *_] = rows_of(db, upload)
    reruns: list[Any] = []
    monkeypatch.setattr(
        categorization_service, "recategorize_after_commit", lambda *args: reruns.append(args)
    )

    assert client.delete(f"/api/v1/uploads/{upload}", headers=alice).status_code == 204
    assert client.delete(f"/api/v1/accounts/{row.account_id}", headers=alice).status_code == 204
    assert reruns == []


# --- Manual Self Transfer rows are anchors (review of Step 8, decided with Dhruv) ----------


def refile(client: TestClient, headers: dict[str, str], row: Transaction, category: str) -> None:
    response = client.patch(
        f"/api/v1/transactions/{row.id}", json={"category_id": category}, headers=headers
    )
    assert response.status_code == 200


@pytest.mark.usefixtures("seeded")
def test_confirming_a_pair_by_hand_keeps_the_other_half(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    savings, other = import_both(client, alice, tmp_path)

    refile(client, alice, rows_of(db, savings)[SENT], system_id(db, "self-transfer"))

    assert state(db, rows_of(db, savings)[SENT]) == (
        "self-transfer",
        CategorySource.USER_MANUAL,
        True,
    )
    assert state(db, rows_of(db, other)[0]) == PAIRED  # was stripped before the fix


@pytest.mark.usefixtures("seeded")
def test_a_transfer_marked_by_hand_finds_its_other_half(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    """A rule keeps the credit out of detection; filing it under Self
    Transfer by hand makes it an anchor, and its debit is paired to it."""
    rule = client.post(
        "/api/v1/rules",
        json={
            "match_type": "vpa",
            "pattern": "9999999998@ybl",
            "category_id": system_id(db, "salary"),
        },
        headers=alice,
    )
    assert rule.status_code == 201
    savings, other = import_both(client, alice, tmp_path)
    assert state(db, rows_of(db, savings)[SENT])[2] is False  # nothing to pair with

    refile(client, alice, rows_of(db, other)[0], system_id(db, "self-transfer"))

    assert state(db, rows_of(db, savings)[SENT]) == PAIRED
    # The anchor itself is exactly as the user set it.
    assert state(db, rows_of(db, other)[0]) == ("self-transfer", CategorySource.USER_MANUAL, True)


@pytest.mark.usefixtures("seeded")
def test_unmarking_an_anchor_releases_its_partner(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    savings, other = import_both(client, alice, tmp_path)
    received = rows_of(db, other)[0]
    refile(client, alice, received, system_id(db, "self-transfer"))
    assert state(db, rows_of(db, savings)[SENT]) == PAIRED

    refile(client, alice, received, system_id(db, "salary"))

    assert state(db, rows_of(db, savings)[SENT]) == (
        "transfers-to-people",
        CategorySource.HEURISTIC,
        False,
    )
