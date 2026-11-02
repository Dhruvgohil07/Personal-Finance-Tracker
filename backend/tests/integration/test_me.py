"""GET /me and DELETE /me end to end: real app, real Postgres (ADR 021).

The rules defended here:

- GET /me shows the caller's own profile, never another user's, and never
  the password hash
- DELETE /me needs the current password; a wrong one changes nothing
- after a delete there are ZERO rows of the user in EVERY table, while
  another user's data is untouched (the table list comes from the models,
  so a table added later is checked automatically, or fails the test until
  someone decides how it is covered)
- the delete is recorded in `audit_log`, with no email in it
- every session ends: the old refresh token and access token are refused
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from rq import Queue
from sqlalchemy import Table, func, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.seed import run_seed
from app.models import AuditAction, AuditLog, Category, CategoryKind, StatementUpload, User
from app.models.audit_log import MAX_USER_AGENT_LENGTH
from app.workers import jobs
from tests.fixtures.icici_xls import build_icici_xls

pytestmark = pytest.mark.integration

ME = "/api/v1/me"
LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"
UPLOADS = "/api/v1/uploads"
ACCOUNTS = "/api/v1/accounts"
RULES = "/api/v1/rules"

# The password make_auth_headers registers every user with.
PASSWORD = "correct horse battery"
ALICE = "alice@example.com"
BOB = "bob@example.com"

AuthHeadersFactory = Callable[[str], dict[str, str]]

# Tables that hold no user's data at all: the global merchant dictionary.
GLOBAL_TABLES = {"merchants"}


@pytest.fixture
def alice(make_auth_headers: AuthHeadersFactory) -> dict[str, str]:
    return make_auth_headers(ALICE)


@pytest.fixture
def bob(make_auth_headers: AuthHeadersFactory) -> dict[str, str]:
    return make_auth_headers(BOB)


def user_id_of(db: Session, email: str) -> Any:
    return db.scalar(select(User.id).where(User.email == email))


def delete_me(client: TestClient, headers: dict[str, str], password: str = PASSWORD) -> Any:
    # client.delete() takes no body, so the generic request() is used.
    return client.request("DELETE", ME, json={"password": password}, headers=headers)


# --- GET /me ----------------------------------------------------------------


def test_get_me_shows_your_own_profile(
    client: TestClient, alice: dict[str, str], db: Session
) -> None:
    response = client.get(ME, headers=alice)

    assert response.status_code == 200
    assert response.json() == {
        "id": str(user_id_of(db, ALICE)),
        "email": ALICE,
        "name": "Test",
        # A new user's defaults (DEFAULT_USER_SETTINGS).
        "settings": {"ai_enabled": True, "weekly_email": False},
    }


def test_each_user_sees_only_their_own_profile(
    client: TestClient, alice: dict[str, str], bob: dict[str, str]
) -> None:
    assert client.get(ME, headers=alice).json()["email"] == ALICE
    assert client.get(ME, headers=bob).json()["email"] == BOB


def test_get_me_ignores_unknown_keys_in_the_settings_column(
    client: TestClient, alice: dict[str, str], db: Session
) -> None:
    """Only the settings the API documents are passed on to the browser."""
    user = db.scalar(select(User).where(User.email == ALICE))
    assert user is not None
    user.settings = {**user.settings, "internal_flag": "x"}
    db.commit()

    settings = client.get(ME, headers=alice).json()["settings"]

    assert settings == {"ai_enabled": True, "weekly_email": False}


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_me_needs_a_token(client: TestClient, method: str) -> None:
    response = client.request(method, ME, json={"password": PASSWORD})

    assert response.status_code == 401


# --- DELETE /me: refused ----------------------------------------------------


def test_a_wrong_password_changes_nothing(
    client: TestClient, alice: dict[str, str], db: Session
) -> None:
    response = delete_me(client, alice, password="not my password")

    # 400, not 401: a 401 would make the frontend refresh its token (ADR 020).
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INCORRECT_PASSWORD"
    # The cookie is NOT cleared: the session goes on.
    assert "set-cookie" not in response.headers
    assert user_id_of(db, ALICE) is not None
    assert db.scalar(select(func.count()).select_from(AuditLog)) == 0
    # The token still works.
    assert client.get(ME, headers=alice).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {},  # no password
        {"password": PASSWORD, "confirm": True},  # an unknown field
        {"password": "x" * 129},  # longer than any password we accept
    ],
)
def test_an_invalid_body_is_a_422(
    client: TestClient, alice: dict[str, str], db: Session, body: dict[str, Any]
) -> None:
    response = client.request("DELETE", ME, json=body, headers=alice)

    assert response.status_code == 422
    assert user_id_of(db, ALICE) is not None


def test_password_guesses_are_rate_limited(client: TestClient, alice: dict[str, str]) -> None:
    """5 per minute per user (REAUTH_LIMIT): a stolen token can't brute-force."""
    for _ in range(5):
        assert delete_me(client, alice, password="guess").status_code == 400

    response = delete_me(client, alice)

    # Even the right password is refused once the limit is used up.
    assert response.status_code == 429


# --- DELETE /me: done -------------------------------------------------------


def test_delete_answers_204_and_clears_the_cookie(
    client: TestClient, alice: dict[str, str]
) -> None:
    response = delete_me(client, alice)

    assert response.status_code == 204
    # Deleting a cookie = setting it again, already expired, with the same
    # path it was set with (only then does the browser match it).
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith("refresh_token=")
    assert "max-age=0" in cookie
    assert "path=/api/v1/auth" in cookie


def test_every_session_ends(client: TestClient, alice: dict[str, str]) -> None:
    """The refresh token of another login and the access token stop working."""
    login = client.post(LOGIN, json={"email": ALICE, "password": PASSWORD})
    old_refresh_token = login.cookies["refresh_token"]
    client.cookies.clear()

    assert delete_me(client, alice).status_code == 204

    refresh = client.post(REFRESH, headers={"Cookie": f"refresh_token={old_refresh_token}"})
    assert refresh.status_code == 401
    assert client.get(ME, headers=alice).status_code == 401


def test_the_delete_is_audited_without_the_email(
    client: TestClient, alice: dict[str, str], db: Session
) -> None:
    user_id = user_id_of(db, ALICE)

    delete_me(client, alice)

    [row] = db.scalars(select(AuditLog)).all()
    assert row.user_id == user_id
    assert row.action is AuditAction.USER_DELETED
    assert row.entity == "user"
    assert row.entity_id == user_id
    # TestClient's own client address and User-Agent.
    assert row.ip == "testclient"
    assert row.user_agent == "testclient"
    assert row.created_at is not None
    # No column of the row holds the email (or any part of the profile).
    values = [str(getattr(row, column.key)) for column in AuditLog.__table__.columns]
    assert not any(ALICE in value for value in values)


def test_a_huge_user_agent_is_cut(client: TestClient, alice: dict[str, str], db: Session) -> None:
    client.request(
        "DELETE",
        ME,
        json={"password": PASSWORD},
        headers={**alice, "User-Agent": "A" * 5000},
    )

    row = db.scalars(select(AuditLog)).one()
    assert row.user_agent == "A" * MAX_USER_AGENT_LENGTH


def test_the_email_can_be_used_again(client: TestClient, alice: dict[str, str]) -> None:
    """Nothing of the old user is left, not even the email's unique key."""
    delete_me(client, alice)

    response = client.post(
        "/api/v1/auth/register", json={"email": ALICE, "password": PASSWORD, "name": "New"}
    )

    assert response.status_code == 201


# --- DELETE /me: every table ------------------------------------------------


def give_data(
    client: TestClient, db: Session, headers: dict[str, str], email: str, tmp_path: Path
) -> None:
    """Put a row of this user into every table that can hold their data.

    Runs on the held queue: the first upload is imported by hand (so it has
    transactions), the second one stays `pending` with its file stored.
    """
    account = client.post(
        ACCOUNTS,
        json={
            "bank_code": "ICICI",
            "nickname": "Salary account",
            "account_type": "savings",
            "masked_number": "1234",
        },
        headers=headers,
    )
    assert account.status_code == 201
    account_id = account.json()["id"]

    upload_ids = []
    # Two different files (different periods), so the second is not a
    # duplicate of the first.
    for name, period_to in (("august.xls", "31/08/2026"), ("august-2.xls", "30/09/2026")):
        content = build_icici_xls(tmp_path / f"{email}-{name}", period_to=period_to).read_bytes()
        response = client.post(
            UPLOADS,
            headers=headers,
            files={"file": (name, content, "application/octet-stream")},
            data={"account_id": account_id},
        )
        assert response.status_code == 202
        upload_ids.append(response.json()["id"])
    jobs.process_statement(upload_ids[0])

    own = Category(
        user_id=user_id_of(db, email), name="Coffee", slug="coffee", kind=CategoryKind.EXPENSE
    )
    db.add(own)
    db.commit()
    rule = client.post(
        RULES,
        json={"match_type": "contains", "pattern": "SALARY", "category_id": str(own.id)},
        headers=headers,
    )
    assert rule.status_code == 201


def rows_of(db: Session, table: Table, user_id: Any) -> int:
    """How many rows of `table` belong to the user."""
    if table.name == "users":
        condition = table.c.id == user_id
    elif "user_id" in table.c:
        condition = table.c.user_id == user_id
    elif table.name == "upload_blobs":
        # A blob belongs to whoever owns its upload.
        condition = table.c.upload_id.in_(
            select(StatementUpload.id).where(StatementUpload.user_id == user_id)
        )
    else:
        pytest.fail(
            f"{table.name}: a new table. Decide how DELETE /me removes its rows "
            "(a user_id FK with ON DELETE CASCADE?) and teach this test about it."
        )
    return db.scalar(select(func.count()).select_from(table).where(condition)) or 0


def total_rows(db: Session, table: Table) -> int:
    return db.scalar(select(func.count()).select_from(table)) or 0


def test_delete_removes_every_row_of_the_user_and_nothing_else(
    client: TestClient,
    held_queue: Queue,
    db: Session,
    make_auth_headers: AuthHeadersFactory,
    tmp_path: Path,
) -> None:
    run_seed(db)  # system categories and merchants
    alice = make_auth_headers(ALICE)
    bob = make_auth_headers(BOB)
    give_data(client, db, alice, ALICE, tmp_path)
    give_data(client, db, bob, BOB, tmp_path)
    alice_id, bob_id = user_id_of(db, ALICE), user_id_of(db, BOB)

    user_tables = [
        table
        for table in Base.metadata.sorted_tables
        if table.name not in GLOBAL_TABLES and table.name != "audit_log"
    ]
    before = {
        table.name: (
            rows_of(db, table, alice_id),
            rows_of(db, table, bob_id),
            total_rows(db, table),
        )
        for table in user_tables
    }
    # Not a vacuous test: Alice really had rows in every one of them.
    assert all(alice_rows > 0 for alice_rows, _, _ in before.values()), before

    assert delete_me(client, alice).status_code == 204

    db.expire_all()  # read what the request committed, not a cache
    for table in user_tables:
        alice_rows, bob_rows, total = before[table.name]
        assert rows_of(db, table, alice_id) == 0, table.name
        assert rows_of(db, table, bob_id) == bob_rows, table.name
        # Nothing else went: system categories, for example, are all still there.
        assert total_rows(db, table) == total - alice_rows, table.name
    # Bob is still fully usable.
    assert client.get(ME, headers=bob).status_code == 200


def test_a_job_still_waiting_after_the_delete_does_nothing(
    client: TestClient, held_queue: Queue, alice: dict[str, str], db: Session, tmp_path: Path
) -> None:
    """The worker may pick up an upload's job after its owner is gone."""
    account = client.post(
        ACCOUNTS,
        json={
            "bank_code": "ICICI",
            "nickname": "Salary account",
            "account_type": "savings",
            "masked_number": "1234",
        },
        headers=alice,
    )
    content = build_icici_xls(tmp_path / "s.xls").read_bytes()
    response = client.post(
        UPLOADS,
        headers=alice,
        files={"file": ("s.xls", content, "application/octet-stream")},
        data={"account_id": account.json()["id"]},
    )
    upload_id = response.json()["id"]
    [job] = held_queue.get_jobs()
    assert job.args == (upload_id,)

    assert delete_me(client, alice).status_code == 204
    # What the worker would run, in this process: no error, nothing created.
    jobs.process_statement(upload_id)

    assert total_rows(db, StatementUpload.__table__) == 0
