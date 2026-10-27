"""User rules end to end (Phase 2 Step 7, SPEC §8.1, ADR 017).

- `/rules` CRUD, with the IDOR 404 on every route that takes an id
- every rule change re-categorizes the user's past rows, except manual ones
- `PATCH /transactions/{id}` with `apply_to_similar=true` learns a rule
- the job's arguments, and what happens when Redis is down

Every job runs synchronously on the test queue, so when a request returns,
the re-categorization it enqueued has already happened.

Narrations use the real ICICI layouts with invented names, VPAs and
reference numbers (CLAUDE.md rule 8).
"""

import uuid
from pathlib import Path
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from rq import Queue
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.categorizer.base import CategorySource
from app.categorizer.handlers import RuleMatchType
from app.core.redis import get_redis
from app.db.seed import run_seed
from app.models import Category, CategoryKind, CategoryRule, Transaction, User
from app.schemas.rule import RuleCreate
from app.services import rules as rules_service
from app.services.categorization import recategorize_user
from app.workers.queue import RECATEGORIZE_USER, enqueue_recategorization
from tests.integration.conftest import TEST_QUEUE_NAME
from tests.integration.test_categorization import upi
from tests.integration.test_uploads import (
    AuthHeadersFactory,
    import_file,
    make_account,
    statement_bytes,
    transactions_of,
)

pytestmark = pytest.mark.integration

RULES = "/api/v1/rules"

# (S No, value date, txn date, cheque, remarks, withdrawal, deposit, balance)
ROWS = (
    # 0, 1: one person, paid twice (the P2P heuristic files both)
    ("1", "01/08/2026", "01/08/2026", "",
     upi("ASHA VERMA", "9876543210@upi", "Sent using", "STATE BANK", "512345678901"),
     "500.00", "0.00", "99500.00"),
    ("2", "02/08/2026", "02/08/2026", "",
     upi("ASHA VERMA", "9876543210@upi", "rent", "STATE BANK", "512345678902"),
     "300.00", "0.00", "99200.00"),
    # 2: a local shop's QR code: nothing built in knows it
    ("3", "03/08/2026", "03/08/2026", "",
     upi("MOON CAFE", "Q123456789@ybl", "NO REMARK", "YES BANK L", "512345678903"),
     "120.00", "0.00", "99080.00"),
    # 3: cash: no merchant, no VPA - nothing a rule could match on
    ("4", "04/08/2026", "04/08/2026", "",
     "NFS/CASH WDL/612345678913/12345678/MUMBAI CEN/04-08-261430",
     "2000.00", "0.00", "97080.00"),
    # 4: Swiggy, a dictionary merchant
    ("5", "05/08/2026", "05/08/2026", "",
     upi("Swiggy Lim", "swiggypayu@hdf", "NO REMARK", "HDFC BANK", "512345678905"),
     "450.00", "0.00", "96630.00"),
)  # fmt: skip
ASHA_1, ASHA_2, MOON, CASH, SWIGGY = range(5)


@pytest.fixture(autouse=True)
def seeded(db: Session) -> None:
    """System categories and the merchant dictionary (the `db` fixture truncates)."""
    run_seed(db)


@pytest.fixture
def alice(make_auth_headers: AuthHeadersFactory) -> dict[str, str]:
    return make_auth_headers("alice@example.com")


@pytest.fixture
def bob(make_auth_headers: AuthHeadersFactory) -> dict[str, str]:
    return make_auth_headers("bob@example.com")


def system_id(db: Session, slug: str) -> str:
    category_id = db.scalar(
        select(Category.id).where(Category.slug == slug, Category.user_id.is_(None))
    )
    assert category_id is not None
    return str(category_id)


def own_category(db: Session, email: str, slug: str = "coffee") -> str:
    user_id = db.scalar(select(User.id).where(User.email == email))
    category = Category(user_id=user_id, name=slug.title(), slug=slug, kind=CategoryKind.EXPENSE)
    db.add(category)
    db.commit()
    return str(category.id)


def import_rows(client: TestClient, headers: dict[str, str], tmp_path: Path) -> str:
    account_id = make_account(client, headers)
    body = import_file(
        client, headers, content=statement_bytes(tmp_path, rows=ROWS), account_id=account_id
    )
    assert body["status"] == "completed"
    upload_id: str = body["id"]
    return upload_id


def rows_of(db: Session, upload_id: str) -> list[Transaction]:
    db.expire_all()  # read what the requests and jobs committed, not a cache
    return transactions_of(db, upload_id)


def state(db: Session, row: Transaction) -> tuple[str | None, CategorySource]:
    """(category slug, source) - the two things the tests below care about."""
    slug = None
    if row.category_id is not None:
        slug = db.scalar(select(Category.slug).where(Category.id == row.category_id))
    return slug, row.category_source


def create_rule(client: TestClient, headers: dict[str, str], **body: Any) -> dict[str, Any]:
    response = client.post(RULES, json=body, headers=headers)
    assert response.status_code == 201, response.json()
    rule: dict[str, Any] = response.json()
    return rule


# --- CRUD -----------------------------------------------------------------------


def test_create_list_update_delete(client: TestClient, alice: dict[str, str], db: Session) -> None:
    groceries, rent = system_id(db, "groceries"), system_id(db, "rent")

    created = create_rule(
        client, alice, match_type="merchant", pattern="Moon Cafe", category_id=groceries
    )
    assert created["pattern"] == "MOONCAFE"  # stored normalized
    assert created["priority"] == 100
    assert created["created_from_transaction_id"] is None

    response = client.patch(
        f"{RULES}/{created['id']}", json={"category_id": rent, "priority": 5}, headers=alice
    )
    assert response.status_code == 200
    assert (response.json()["category_id"], response.json()["priority"]) == (rent, 5)

    listed = client.get(RULES, headers=alice).json()
    assert [rule["id"] for rule in listed] == [created["id"]]

    assert client.delete(f"{RULES}/{created['id']}", headers=alice).status_code == 204
    assert client.get(RULES, headers=alice).json() == []


def test_rules_are_listed_in_the_order_they_apply(
    client: TestClient, alice: dict[str, str], db: Session
) -> None:
    groceries = system_id(db, "groceries")
    older = create_rule(client, alice, match_type="contains", pattern="moon", category_id=groceries)
    newer = create_rule(client, alice, match_type="contains", pattern="cafe", category_id=groceries)
    first = create_rule(
        client, alice, match_type="contains", pattern="tea", category_id=groceries, priority=1
    )

    listed = [rule["id"] for rule in client.get(RULES, headers=alice).json()]

    # Lowest number first; among equals, the newest first.
    assert listed == [first["id"], newer["id"], older["id"]]


def test_a_second_rule_for_the_same_match_is_a_409(
    client: TestClient, alice: dict[str, str], db: Session
) -> None:
    groceries = system_id(db, "groceries")
    create_rule(client, alice, match_type="merchant", pattern="Moon Cafe", category_id=groceries)

    response = client.post(
        RULES,
        json={"match_type": "merchant", "pattern": "MOONCAFE", "category_id": groceries},
        headers=alice,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


def test_the_same_pattern_under_another_match_type_is_a_different_rule(
    client: TestClient, alice: dict[str, str], db: Session
) -> None:
    groceries = system_id(db, "groceries")
    create_rule(client, alice, match_type="merchant", pattern="moon cafe", category_id=groceries)

    create_rule(client, alice, match_type="contains", pattern="moon cafe", category_id=groceries)


def test_a_user_may_use_their_own_category(
    client: TestClient, alice: dict[str, str], db: Session
) -> None:
    coffee = own_category(db, "alice@example.com")

    rule = create_rule(client, alice, match_type="contains", pattern="moon", category_id=coffee)

    assert rule["category_id"] == coffee


def test_a_non_duplicate_integrity_error_is_not_a_409(db: Session) -> None:
    """Only the rules' own unique constraint means "duplicate". A foreign-key
    failure (the user was deleted mid-request) travels on as a 500, the same
    mapping as accounts (test_accounts.py)."""
    body = RuleCreate(
        match_type=RuleMatchType.CONTAINS,
        pattern="moon",
        category_id=uuid.UUID(system_id(db, "groceries")),
    )
    # Never used: the commit fails before anything is enqueued.
    queue = Queue(TEST_QUEUE_NAME, connection=get_redis())

    with pytest.raises(IntegrityError) as excinfo:
        rules_service.create_rule(db, queue, uuid.uuid4(), body)

    assert excinfo.value.orig is not None
    assert excinfo.value.orig.diag.constraint_name == "fk_category_rules_user_id_users"


def test_a_rule_needs_auth(client: TestClient) -> None:
    assert client.get(RULES).status_code == 401
    assert client.post(RULES, json={}).status_code == 401


# --- IDOR (CLAUDE.md rule 2) ----------------------------------------------------------


def test_another_users_rule_is_a_404_on_every_route(
    client: TestClient, alice: dict[str, str], bob: dict[str, str], db: Session
) -> None:
    groceries = system_id(db, "groceries")
    alices = create_rule(
        client, alice, match_type="contains", pattern="moon", category_id=groceries
    )
    url = f"{RULES}/{alices['id']}"

    assert client.get(RULES, headers=bob).json() == []
    patched = client.patch(url, json={"priority": 1}, headers=bob)
    assert patched.status_code == 404
    assert client.delete(url, headers=bob).status_code == 404
    # ... and it is untouched.
    [still] = client.get(RULES, headers=alice).json()
    assert still["priority"] == 100


def test_another_users_category_is_a_404(
    client: TestClient, alice: dict[str, str], bob: dict[str, str], db: Session
) -> None:
    bobs = own_category(db, "bob@example.com")
    groceries = system_id(db, "groceries")

    created = client.post(
        RULES,
        json={"match_type": "contains", "pattern": "moon", "category_id": bobs},
        headers=alice,
    )
    assert created.status_code == 404
    rule = create_rule(client, alice, match_type="contains", pattern="moon", category_id=groceries)
    patched = client.patch(f"{RULES}/{rule['id']}", json={"category_id": bobs}, headers=alice)
    assert patched.status_code == 404
    assert client.get(RULES, headers=alice).json()[0]["category_id"] == groceries


# --- A rule change re-categorizes the past -----------------------------------------------


def test_a_new_rule_recategorizes_past_rows(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    upload_id = import_rows(client, alice, tmp_path)
    assert state(db, rows_of(db, upload_id)[MOON]) == (None, CategorySource.NONE)

    create_rule(
        client,
        alice,
        match_type="merchant",
        pattern="moon cafe",
        category_id=system_id(db, "food-dining"),
    )

    assert state(db, rows_of(db, upload_id)[MOON]) == ("food-dining", CategorySource.USER_RULE)


def test_a_rule_beats_the_built_in_guesses_and_deleting_it_brings_them_back(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    upload_id = import_rows(client, alice, tmp_path)
    rule = create_rule(
        client, alice, match_type="vpa", pattern="9876543210@UPI", category_id=system_id(db, "rent")
    )
    rows = rows_of(db, upload_id)
    assert state(db, rows[ASHA_1]) == ("rent", CategorySource.USER_RULE)
    assert state(db, rows[ASHA_2]) == ("rent", CategorySource.USER_RULE)

    assert client.delete(f"{RULES}/{rule['id']}", headers=alice).status_code == 204

    rows = rows_of(db, upload_id)
    assert state(db, rows[ASHA_1]) == ("transfers-to-people", CategorySource.HEURISTIC)
    assert state(db, rows[ASHA_2]) == ("transfers-to-people", CategorySource.HEURISTIC)


def test_a_rule_never_overwrites_a_manual_category(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    upload_id = import_rows(client, alice, tmp_path)
    manual = rows_of(db, upload_id)[ASHA_1]
    response = client.patch(
        f"/api/v1/transactions/{manual.id}",
        json={"category_id": system_id(db, "health")},
        headers=alice,
    )
    assert response.status_code == 200

    create_rule(
        client, alice, match_type="vpa", pattern="9876543210@upi", category_id=system_id(db, "rent")
    )

    rows = rows_of(db, upload_id)
    assert state(db, rows[ASHA_1]) == ("health", CategorySource.USER_MANUAL)
    assert state(db, rows[ASHA_2]) == ("rent", CategorySource.USER_RULE)


def test_changing_the_priority_changes_which_rule_wins(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    upload_id = import_rows(client, alice, tmp_path)
    create_rule(
        client, alice, match_type="contains", pattern="moon", category_id=system_id(db, "groceries")
    )
    by_key = create_rule(
        client, alice, match_type="merchant", pattern="mooncafe", category_id=system_id(db, "rent")
    )
    # Equal priorities: the newest rule (the merchant one) wins.
    assert state(db, rows_of(db, upload_id)[MOON])[0] == "rent"

    client.patch(f"{RULES}/{by_key['id']}", json={"priority": 200}, headers=alice)

    assert state(db, rows_of(db, upload_id)[MOON])[0] == "groceries"


def test_a_merchant_rule_matches_the_key_the_api_shows(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    """How a key is built depends on the bank's layout, so the API shows it
    and a merchant rule matches it exactly (review of Step 7)."""
    import_rows(client, alice, tmp_path)
    items = client.get("/api/v1/transactions", headers=alice).json()["items"]
    moon = next(item for item in items if item["merchant_key"] == "MOONCAFE")

    create_rule(
        client,
        alice,
        match_type="merchant",
        pattern=moon["merchant_key"],
        category_id=system_id(db, "groceries"),
    )

    response = client.get("/api/v1/transactions", headers=alice).json()["items"]
    [refiled] = [item for item in response if item["id"] == moon["id"]]
    assert (refiled["category_id"], refiled["category_source"]) == (
        system_id(db, "groceries"),
        "user_rule",
    )


def test_a_rule_applies_to_future_imports(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    create_rule(
        client,
        alice,
        match_type="merchant",
        pattern="moon cafe",
        category_id=system_id(db, "groceries"),
    )

    upload_id = import_rows(client, alice, tmp_path)

    assert state(db, rows_of(db, upload_id)[MOON]) == ("groceries", CategorySource.USER_RULE)


def test_a_transfer_category_from_a_rule_marks_a_self_transfer(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    """Like a manual choice: the flag the insights read follows the category."""
    upload_id = import_rows(client, alice, tmp_path)
    rule = create_rule(
        client,
        alice,
        match_type="vpa",
        pattern="9876543210@upi",
        category_id=system_id(db, "self-transfer"),
    )
    assert all(row.is_self_transfer for row in rows_of(db, upload_id)[:2])

    client.delete(f"{RULES}/{rule['id']}", headers=alice)

    assert not any(row.is_self_transfer for row in rows_of(db, upload_id))


def test_one_users_rule_never_touches_another_users_rows(
    client: TestClient,
    alice: dict[str, str],
    bob: dict[str, str],
    tmp_path: Path,
    db: Session,
) -> None:
    bobs_upload = import_rows(client, bob, tmp_path)

    create_rule(
        client,
        alice,
        match_type="merchant",
        pattern="moon cafe",
        category_id=system_id(db, "groceries"),
    )

    assert state(db, rows_of(db, bobs_upload)[MOON]) == (None, CategorySource.NONE)


def test_a_rerun_with_the_same_rules_changes_nothing(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    """The job is safe to run twice (SPEC §11)."""
    import_rows(client, alice, tmp_path)
    create_rule(
        client,
        alice,
        match_type="merchant",
        pattern="moon cafe",
        category_id=system_id(db, "groceries"),
    )
    user_id = db.scalar(select(User.id).where(User.email == "alice@example.com"))
    assert user_id is not None

    with structlog.testing.capture_logs() as logs:
        assert recategorize_user(db, user_id) == 0

    [done] = [entry for entry in logs if entry["event"] == "transactions_recategorized"]
    assert (done["rows_count"], done["rows_changed"]) == (5, 0)


# --- apply_to_similar (SPEC §8.1) ---------------------------------------------------------


def patch_similar(
    client: TestClient, headers: dict[str, str], row: Transaction, category_id: str
) -> Any:
    return client.patch(
        f"/api/v1/transactions/{row.id}",
        json={"category_id": category_id, "apply_to_similar": True},
        headers=headers,
    )


def test_apply_to_similar_learns_a_vpa_rule_for_a_person(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    upload_id = import_rows(client, alice, tmp_path)
    rent = system_id(db, "rent")
    corrected = rows_of(db, upload_id)[ASHA_1]

    response = patch_similar(client, alice, corrected, rent)

    assert response.status_code == 200
    [rule] = client.get(RULES, headers=alice).json()
    assert (rule["match_type"], rule["pattern"], rule["category_id"]) == (
        "vpa",
        "9876543210@upi",
        rent,
    )
    assert rule["created_from_transaction_id"] == str(corrected.id)
    rows = rows_of(db, upload_id)
    assert state(db, rows[ASHA_1]) == ("rent", CategorySource.USER_MANUAL)  # chosen by hand
    assert state(db, rows[ASHA_2]) == ("rent", CategorySource.USER_RULE)  # the similar one


def test_apply_to_similar_learns_a_merchant_rule_for_a_shop(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    upload_id = import_rows(client, alice, tmp_path)

    patch_similar(client, alice, rows_of(db, upload_id)[MOON], system_id(db, "food-dining"))

    [rule] = client.get(RULES, headers=alice).json()
    assert (rule["match_type"], rule["pattern"]) == ("merchant", "MOONCAFE")


def test_a_second_correction_updates_the_rule_instead_of_adding_one(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    """The latest correction wins (ADR 017)."""
    upload_id = import_rows(client, alice, tmp_path)
    rows = rows_of(db, upload_id)
    patch_similar(client, alice, rows[ASHA_1], system_id(db, "rent"))

    response = patch_similar(client, alice, rows[ASHA_2], system_id(db, "health"))

    assert response.status_code == 200
    [rule] = client.get(RULES, headers=alice).json()
    assert rule["category_id"] == system_id(db, "health")
    assert rule["created_from_transaction_id"] == str(rows[ASHA_2].id)
    # The first row was chosen by hand, so it keeps its own choice.
    rows = rows_of(db, upload_id)
    assert state(db, rows[ASHA_1]) == ("rent", CategorySource.USER_MANUAL)
    assert state(db, rows[ASHA_2]) == ("health", CategorySource.USER_MANUAL)


def test_a_retaught_rule_beats_a_rule_written_after_it(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    """Ties go to the rule changed most recently, not created (review of Step 7).

    The upsert keeps the original row, so ordering by `created_at` would
    leave a re-taught rule behind a rule written after its first version.
    """
    upload_id = import_rows(client, alice, tmp_path)
    moon = rows_of(db, upload_id)[MOON]
    patch_similar(client, alice, moon, system_id(db, "food-dining"))  # MOONCAFE rule
    newer = create_rule(
        client, alice, match_type="contains", pattern="moon", category_id=system_id(db, "groceries")
    )
    [first, _] = client.get(RULES, headers=alice).json()
    assert first["id"] == newer["id"]

    patch_similar(client, alice, moon, system_id(db, "rent"))  # re-teaches MOONCAFE

    [first, second] = client.get(RULES, headers=alice).json()
    assert (first["match_type"], first["category_id"]) == ("merchant", system_id(db, "rent"))
    assert second["id"] == newer["id"]


def test_apply_to_similar_with_nothing_to_match_changes_nothing(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    upload_id = import_rows(client, alice, tmp_path)
    cash = rows_of(db, upload_id)[CASH]
    before = state(db, cash)

    response = patch_similar(client, alice, cash, system_id(db, "groceries"))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "NOTHING_TO_MATCH"
    # Category and rule are one commit: neither was saved.
    assert state(db, rows_of(db, upload_id)[CASH]) == before
    assert client.get(RULES, headers=alice).json() == []


def test_apply_to_similar_on_another_users_transaction_is_a_404(
    client: TestClient, alice: dict[str, str], bob: dict[str, str], tmp_path: Path, db: Session
) -> None:
    upload_id = import_rows(client, alice, tmp_path)

    response = patch_similar(client, bob, rows_of(db, upload_id)[MOON], system_id(db, "rent"))

    assert response.status_code == 404
    assert db.scalar(select(CategoryRule.id)) is None


def test_deleting_the_upload_keeps_the_learned_rule(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    """`created_from_transaction_id` is SET NULL: the rule the user taught stays."""
    upload_id = import_rows(client, alice, tmp_path)
    patch_similar(client, alice, rows_of(db, upload_id)[MOON], system_id(db, "food-dining"))

    assert client.delete(f"/api/v1/uploads/{upload_id}", headers=alice).status_code == 204

    [rule] = client.get(RULES, headers=alice).json()
    assert rule["created_from_transaction_id"] is None


# --- The job and its queue -------------------------------------------------------------


def test_the_job_carries_only_the_user_id() -> None:
    """Never the rule: a VPA pattern is personal data, and job arguments
    sit in Redis unencrypted. Enqueued on a real (not synchronous) queue
    so the stored job can be read back, then deleted unrun."""
    queue = Queue(TEST_QUEUE_NAME, connection=get_redis())
    user_id = uuid.uuid4()

    job = enqueue_recategorization(queue, user_id)
    try:
        assert job.func_name == RECATEGORIZE_USER
        assert job.args == (str(user_id),)
        assert job.kwargs == {}
    finally:
        job.delete()


def test_with_redis_down_the_rules_are_applied_in_the_request(
    client: TestClient,
    alice: dict[str, str],
    tmp_path: Path,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload_id = import_rows(client, alice, tmp_path)

    def redis_down(queue: Queue, user_id: uuid.UUID) -> None:
        raise RedisConnectionError("down")

    monkeypatch.setattr(rules_service, "enqueue_recategorization", redis_down)

    with structlog.testing.capture_logs() as logs:
        create_rule(
            client,
            alice,
            match_type="merchant",
            pattern="moon cafe",
            category_id=system_id(db, "groceries"),
        )

    assert state(db, rows_of(db, upload_id)[MOON]) == ("groceries", CategorySource.USER_RULE)
    assert any(entry["event"] == "recategorize_enqueue_failed_running_inline" for entry in logs)
