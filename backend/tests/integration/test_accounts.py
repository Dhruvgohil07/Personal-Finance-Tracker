"""Accounts CRUD end to end: real app, real Postgres.

The most important tests here are the IDOR ones (rule 2): user B must get
a 404, never user A's data, for every route that takes an account id.
"""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from app.models import Account, AccountType, BankCode, User
from app.schemas.account import AccountCreate
from app.services import accounts as accounts_service

pytestmark = pytest.mark.integration

ACCOUNTS = "/api/v1/accounts"

# The type of the `make_auth_headers` fixture from conftest.py.
AuthHeadersFactory = Callable[[str], dict[str, str]]

NEW_ACCOUNT: dict[str, Any] = {
    "bank_code": "ICICI",
    "nickname": "Salary account",
    "account_type": "savings",
    "masked_number": "0042",  # leading zero on purpose: must survive as text
}


# --- Helpers / fixtures -----------------------------------------------------


@pytest.fixture
def alice(make_auth_headers: AuthHeadersFactory) -> dict[str, str]:
    return make_auth_headers("alice@example.com")


@pytest.fixture
def bob(make_auth_headers: AuthHeadersFactory) -> dict[str, str]:
    return make_auth_headers("bob@example.com")


def _create(client: TestClient, headers: dict[str, str], **overrides: Any) -> dict[str, Any]:
    response = client.post(ACCOUNTS, json={**NEW_ACCOUNT, **overrides}, headers=headers)
    assert response.status_code == 201
    body: dict[str, Any] = response.json()
    return body


# --- Auth -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", ACCOUNTS),
        ("POST", ACCOUNTS),
        ("PATCH", f"{ACCOUNTS}/{uuid.uuid4()}"),
        ("DELETE", f"{ACCOUNTS}/{uuid.uuid4()}"),
    ],
)
def test_requires_login(client: TestClient, method: str, path: str) -> None:
    response = client.request(method, path, json=NEW_ACCOUNT)

    assert response.status_code == 401


# --- Create -----------------------------------------------------------------


def test_create_account(client: TestClient, alice: dict[str, str], db: Session) -> None:
    body = _create(client, alice)

    assert body["bank_code"] == "ICICI"
    assert body["nickname"] == "Salary account"
    assert body["account_type"] == "savings"
    assert body["masked_number"] == "0042"
    assert set(body) == {
        "id",
        "bank_code",
        "nickname",
        "account_type",
        "masked_number",
        "created_at",
    }

    # Stored for the logged-in user (user_id came from the token).
    account = db.get(Account, uuid.UUID(body["id"]))
    user = db.scalar(select(User).where(User.email == "alice@example.com"))
    assert account is not None and user is not None
    assert account.user_id == user.id


def test_create_duplicate_is_409(client: TestClient, alice: dict[str, str]) -> None:
    _create(client, alice)

    response = client.post(ACCOUNTS, json=NEW_ACCOUNT, headers=alice)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


def test_same_number_at_another_bank_is_allowed(client: TestClient, alice: dict[str, str]) -> None:
    _create(client, alice)
    _create(client, alice, bank_code="HDFC")


def test_savings_and_credit_card_with_same_digits_are_allowed(
    client: TestClient, alice: dict[str, str]
) -> None:
    # Same bank, same last 4 digits, but two different real accounts.
    _create(client, alice, account_type="savings")
    _create(client, alice, account_type="credit_card")


def test_two_users_can_add_the_same_account_number(
    client: TestClient, alice: dict[str, str], bob: dict[str, str]
) -> None:
    # Uniqueness is per user: different people's accounts can share digits.
    _create(client, alice)
    _create(client, bob)


@pytest.mark.parametrize(
    "overrides",
    [
        {"masked_number": "123456789012"},  # a full account number
        {"masked_number": "123"},
        {"masked_number": "12a4"},
        {"masked_number": "１２３４"},  # full-width digits: not [0-9]
        {"bank_code": "AXIS"},  # not a supported bank (yet)
        {"account_type": "loan"},
        {"nickname": "   "},  # blank after stripping
        {"nickname": "x" * 51},
        {"nickname": "Sal\u0000ary"},  # NUL: Postgres can't store it (would be a 500)
        {"nickname": "two\nlines"},  # control character
        {"user_id": str(uuid.uuid4())},  # can't choose the owner
    ],
)
def test_create_invalid_body_is_422(
    client: TestClient, alice: dict[str, str], overrides: dict[str, Any]
) -> None:
    response = client.post(ACCOUNTS, json={**NEW_ACCOUNT, **overrides}, headers=alice)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_rejected_account_number_is_not_echoed(client: TestClient, alice: dict[str, str]) -> None:
    full_number = "918273645501"

    response = client.post(
        ACCOUNTS, json={**NEW_ACCOUNT, "masked_number": full_number}, headers=alice
    )

    assert response.status_code == 422
    assert full_number not in response.text


# --- List -------------------------------------------------------------------


def test_list_returns_own_accounts_oldest_first(
    client: TestClient, alice: dict[str, str], bob: dict[str, str]
) -> None:
    first = _create(client, alice)
    second = _create(client, alice, bank_code="HDFC", masked_number="9999")
    _create(client, bob)

    response = client.get(ACCOUNTS, headers=alice)

    assert response.status_code == 200
    assert [a["id"] for a in response.json()] == [first["id"], second["id"]]


def test_list_is_empty_for_new_user(client: TestClient, alice: dict[str, str]) -> None:
    response = client.get(ACCOUNTS, headers=alice)

    assert response.status_code == 200
    assert response.json() == []


# --- Delete -----------------------------------------------------------------


def test_delete_account(client: TestClient, alice: dict[str, str], db: Session) -> None:
    account = _create(client, alice)

    response = client.delete(f"{ACCOUNTS}/{account['id']}", headers=alice)

    assert response.status_code == 204
    assert db.get(Account, uuid.UUID(account["id"])) is None


def test_delete_unknown_account_is_404(client: TestClient, alice: dict[str, str]) -> None:
    response = client.delete(f"{ACCOUNTS}/{uuid.uuid4()}", headers=alice)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_delete_non_uuid_id_is_422(client: TestClient, alice: dict[str, str]) -> None:
    response = client.delete(f"{ACCOUNTS}/not-a-uuid", headers=alice)

    assert response.status_code == 422


# --- IDOR: another user's account --------------------------------------------


def test_idor_delete_other_users_account_is_404(
    client: TestClient, alice: dict[str, str], bob: dict[str, str], db: Session
) -> None:
    alices_account = _create(client, alice)

    response = client.delete(f"{ACCOUNTS}/{alices_account['id']}", headers=bob)

    # Same answer as for an id that doesn't exist, and nothing deleted.
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert db.get(Account, uuid.UUID(alices_account["id"])) is not None


def test_idor_list_never_shows_other_users_accounts(
    client: TestClient, alice: dict[str, str], bob: dict[str, str]
) -> None:
    _create(client, alice)

    response = client.get(ACCOUNTS, headers=bob)

    assert response.json() == []


def test_idor_patch_other_users_account_is_404(
    client: TestClient, alice: dict[str, str], bob: dict[str, str]
) -> None:
    alices_account = _create(client, alice)

    response = client.patch(
        f"{ACCOUNTS}/{alices_account['id']}", json={"nickname": "Mine now"}, headers=bob
    )

    assert response.status_code == 404
    # Alice's account is unchanged.
    [account] = client.get(ACCOUNTS, headers=alice).json()
    assert account["nickname"] == "Salary account"


# --- Update (PATCH) ---------------------------------------------------------


def test_patch_changes_only_sent_fields(client: TestClient, alice: dict[str, str]) -> None:
    account = _create(client, alice)

    response = client.patch(
        f"{ACCOUNTS}/{account['id']}", json={"nickname": "Old salary"}, headers=alice
    )

    assert response.status_code == 200
    body = response.json()
    assert body["nickname"] == "Old salary"
    assert body["account_type"] == "savings"  # not sent -> unchanged


def test_patch_account_type(client: TestClient, alice: dict[str, str]) -> None:
    account = _create(client, alice)

    response = client.patch(
        f"{ACCOUNTS}/{account['id']}", json={"account_type": "current"}, headers=alice
    )

    assert response.status_code == 200
    assert response.json()["account_type"] == "current"
    assert response.json()["nickname"] == "Salary account"


def test_patch_empty_body_changes_nothing(client: TestClient, alice: dict[str, str]) -> None:
    account = _create(client, alice)

    response = client.patch(f"{ACCOUNTS}/{account['id']}", json={}, headers=alice)

    assert response.status_code == 200
    assert response.json()["nickname"] == "Salary account"


@pytest.mark.parametrize(
    "body",
    [
        {"nickname": None},  # nickname is required in the DB
        {"masked_number": "1111"},  # identifies the account: not changeable
        {"bank_code": "HDFC"},
        {"account_type": "loan"},
        # A valid change next to an invalid one: the whole body is rejected.
        {"nickname": "New name", "masked_number": "1111"},
    ],
)
def test_patch_invalid_body_is_422(
    client: TestClient, alice: dict[str, str], body: dict[str, Any]
) -> None:
    account = _create(client, alice)

    response = client.patch(f"{ACCOUNTS}/{account['id']}", json=body, headers=alice)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    # Nothing was saved, not even part of the body.
    [stored] = client.get(ACCOUNTS, headers=alice).json()
    assert stored == account


def test_patch_onto_an_existing_account_is_409(client: TestClient, alice: dict[str, str]) -> None:
    # account_type is part of the unique key, so changing it can collide with
    # another of the user's accounts. That is a 409, not a 500.
    savings = _create(client, alice, account_type="savings")
    card = _create(client, alice, account_type="credit_card")

    response = client.patch(
        f"{ACCOUNTS}/{card['id']}", json={"account_type": "savings"}, headers=alice
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"
    # Both accounts are unchanged.
    stored = {a["id"]: a for a in client.get(ACCOUNTS, headers=alice).json()}
    assert stored[savings["id"]]["account_type"] == "savings"
    assert stored[card["id"]]["account_type"] == "credit_card"


def test_patch_unknown_account_is_404(client: TestClient, alice: dict[str, str]) -> None:
    response = client.patch(f"{ACCOUNTS}/{uuid.uuid4()}", json={"nickname": "x"}, headers=alice)

    assert response.status_code == 404


# --- Database rules (defence in depth, below the API) ------------------------


def _make_user(db: Session) -> User:
    user = User(email="db@example.com", password_hash="not-a-real-hash", name="Test")
    db.add(user)
    db.commit()
    return user


def _add_account(db: Session, user: User, masked_number: str = "1234") -> None:
    db.add(
        Account(
            user_id=user.id,
            bank_code=BankCode.ICICI,
            nickname="x",
            account_type=AccountType.SAVINGS,
            masked_number=masked_number,
        )
    )


@pytest.mark.parametrize("masked_number", ["123", "12a4", "1 34"])
def test_db_check_constraint_rejects_masked_number(db: Session, masked_number: str) -> None:
    # Even if the API validation had a bug, Postgres' CHECK constraint
    # refuses to store anything but 4 digits. (VARCHAR(4) alone would not
    # catch these.) We assert the constraint's NAME, so the test fails if the
    # constraint is ever dropped and the insert fails for another reason.
    _add_account(db, _make_user(db), masked_number)

    with pytest.raises(IntegrityError) as excinfo:
        db.commit()

    assert excinfo.value.orig is not None
    assert excinfo.value.orig.diag.constraint_name == "ck_accounts_masked_number_4_digits"


def test_db_rejects_masked_number_longer_than_4(db: Session) -> None:
    # Too long fails even earlier, on the column type VARCHAR(4).
    _add_account(db, _make_user(db), "123456789012")

    with pytest.raises(DataError):
        db.commit()


def test_non_duplicate_integrity_error_is_not_a_409(db: Session) -> None:
    # A foreign-key failure (user doesn't exist, e.g. deleted mid-request)
    # must NOT be reported as "you already added this account": only the
    # unique constraint means duplicate. It is re-raised (-> 500).
    data = AccountCreate(
        bank_code=BankCode.ICICI,
        nickname="x",
        account_type=AccountType.SAVINGS,
        masked_number="1234",
    )

    with pytest.raises(IntegrityError) as excinfo:
        accounts_service.create_account(db, uuid.uuid4(), data)

    assert excinfo.value.orig is not None
    assert excinfo.value.orig.diag.constraint_name == "fk_accounts_user_id_users"


def test_deleting_user_deletes_their_accounts(db: Session) -> None:
    user = _make_user(db)
    _add_account(db, user)
    db.commit()

    db.delete(user)
    db.commit()

    assert db.scalars(select(Account)).all() == []
