"""The auth flow end to end: real app, real Postgres (SPEC §13).

Covers register, login, `get_current_user`, refresh rotation, reuse
detection and logout.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from app.api.deps import CurrentUser
from app.core.security import create_access_token
from app.models import RefreshToken, User
from app.services.auth import REUSE_GRACE_PERIOD

pytestmark = pytest.mark.integration

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"
LOGOUT = "/api/v1/auth/logout"
WHOAMI = "/test/whoami"

EMAIL = "dhruv@example.com"
PASSWORD = "correct horse battery"


# --- Helpers ----------------------------------------------------------------


def _register(client: TestClient, email: str = EMAIL, password: str = PASSWORD) -> Response:
    return client.post(REGISTER, json={"email": email, "password": password, "name": "Dhruv"})


def _login(client: TestClient, email: str = EMAIL, password: str = PASSWORD) -> Response:
    return client.post(LOGIN, json={"email": email, "password": password})


def _registered_and_logged_in(client: TestClient) -> tuple[str, str]:
    """Register + log in; return (access_token, refresh_token)."""
    _register(client)
    response = _login(client)
    assert response.status_code == 200
    return response.json()["access_token"], response.cookies["refresh_token"]


def _post_with_refresh_cookie(client: TestClient, url: str, refresh_token: str) -> Response:
    """Send exactly this refresh token, like a browser (or a thief) holding it.

    The client keeps cookies between requests, like a browser. Clearing them
    first makes sure only the token we choose is sent.
    """
    client.cookies.clear()
    return client.post(url, headers={"Cookie": f"refresh_token={refresh_token}"})


def _move_revocations_past_grace_period(db: Session) -> None:
    """Pretend every revocation happened a minute ago.

    A token replayed within REUSE_GRACE_PERIOD of its rotation is treated
    as a harmless race, not theft. Tests of real reuse detection move the
    revoke time back so the replay is clearly "later".
    """
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.revoked_at.is_not(None))
        .values(revoked_at=RefreshToken.revoked_at - REUSE_GRACE_PERIOD - timedelta(minutes=1))
    )
    db.commit()


@pytest.fixture
def protected_client(client: TestClient) -> TestClient:
    """The client, plus a tiny route protected by `CurrentUser`.

    No real protected route exists yet (accounts arrive in Step 4), so we
    add one just for these tests.
    """

    def whoami(user: CurrentUser) -> dict[str, str]:
        return {"user_id": str(user.id)}

    app = client.app
    assert isinstance(app, FastAPI)
    app.add_api_route(WHOAMI, whoami)
    return client


# --- Register ---------------------------------------------------------------


def test_register_creates_user_with_hashed_password(client: TestClient, db: Session) -> None:
    response = _register(client)

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == EMAIL
    assert body["name"] == "Dhruv"
    assert set(body) == {"id", "email", "name"}  # no hash, no settings

    user = db.scalar(select(User).where(User.email == EMAIL))
    assert user is not None
    assert user.password_hash.startswith("$argon2id$")
    assert PASSWORD not in user.password_hash


def test_register_duplicate_email_ignoring_case_is_409(client: TestClient) -> None:
    _register(client, email="dhruv@example.com")

    response = _register(client, email="Dhruv@Example.com")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


@pytest.mark.parametrize(
    "body",
    [
        {"email": "not-an-email", "password": PASSWORD, "name": "Dhruv"},
        {"email": EMAIL, "password": "short", "name": "Dhruv"},  # < 8 characters
        {"email": EMAIL, "password": PASSWORD, "name": "   "},  # blank after stripping
        {
            "email": EMAIL,
            "password": PASSWORD,
            "name": "Dh\u0000ruv",
        },  # NUL: Postgres can't store it
        {"email": EMAIL, "password": PASSWORD, "name": "Dh\nruv"},  # control character
        {"email": EMAIL, "password": PASSWORD},  # name missing
    ],
)
def test_register_rejects_invalid_input(client: TestClient, body: dict[str, str]) -> None:
    response = client.post(REGISTER, json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# --- Login ------------------------------------------------------------------


def test_login_returns_access_token_and_sets_refresh_cookie(client: TestClient) -> None:
    _register(client)

    response = _login(client)

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 15 * 60
    assert "refresh_token" not in body  # only ever in the cookie

    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=strict" in cookie
    assert "path=/api/v1/auth" in cookie


def test_login_email_is_case_insensitive(client: TestClient) -> None:
    _register(client, email="dhruv@example.com")

    assert _login(client, email="DHRUV@example.com").status_code == 200


def test_wrong_password_and_unknown_email_get_the_same_answer(client: TestClient) -> None:
    _register(client)

    wrong_password = _login(client, password="wrong password")
    unknown_email = _login(client, email="nobody@example.com")

    # Identical answers: the client can't tell which emails are registered.
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()
    assert wrong_password.json()["error"]["message"] == "Invalid email or password."


def test_login_logs_contain_no_email_or_password(client: TestClient) -> None:
    _register(client)

    # capture_logs() collects structlog events as dicts instead of printing
    # them (before our redaction runs, so this checks what the CODE logs).
    with capture_logs() as logs:
        _login(client)
        _login(client, password="wrong password")

    logged = str(logs)
    assert EMAIL not in logged
    assert PASSWORD not in logged
    assert "wrong password" not in logged


# --- get_current_user -------------------------------------------------------


def test_protected_route_accepts_valid_access_token(protected_client: TestClient) -> None:
    access_token, _ = _registered_and_logged_in(protected_client)

    response = protected_client.get(WHOAMI, headers={"Authorization": f"Bearer {access_token}"})

    assert response.status_code == 200
    assert uuid.UUID(response.json()["user_id"])


@pytest.mark.parametrize(
    "headers",
    [
        {},  # no Authorization header at all
        {"Authorization": "Bearer not-a-jwt"},
        {"Authorization": "Basic dXNlcjpwYXNz"},  # wrong scheme
    ],
)
def test_protected_route_rejects_missing_or_garbage_token(
    protected_client: TestClient, headers: dict[str, str]
) -> None:
    response = protected_client.get(WHOAMI, headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_protected_route_rejects_expired_access_token(
    protected_client: TestClient, db: Session
) -> None:
    _register(protected_client)
    user = db.scalar(select(User).where(User.email == EMAIL))
    assert user is not None
    expired = create_access_token(user.id, now=datetime.now(UTC) - timedelta(hours=1))

    response = protected_client.get(WHOAMI, headers={"Authorization": f"Bearer {expired}"})

    assert response.status_code == 401


def test_protected_route_rejects_token_of_deleted_user(
    protected_client: TestClient, db: Session
) -> None:
    access_token, _ = _registered_and_logged_in(protected_client)
    db.execute(User.__table__.delete())
    db.commit()

    response = protected_client.get(WHOAMI, headers={"Authorization": f"Bearer {access_token}"})

    assert response.status_code == 401


# --- Refresh ----------------------------------------------------------------


def test_refresh_rotates_the_refresh_token(protected_client: TestClient, db: Session) -> None:
    _, first_token = _registered_and_logged_in(protected_client)

    response = _post_with_refresh_cookie(protected_client, REFRESH, first_token)

    assert response.status_code == 200
    second_token = response.cookies["refresh_token"]
    assert second_token != first_token

    # The new access token works.
    new_access = response.json()["access_token"]
    whoami = protected_client.get(WHOAMI, headers={"Authorization": f"Bearer {new_access}"})
    assert whoami.status_code == 200

    # Both tokens are in the same family; only the old one is revoked.
    rows = db.scalars(select(RefreshToken)).all()
    assert len(rows) == 2
    assert len({row.family_id for row in rows}) == 1
    assert sorted(row.revoked_at is None for row in rows) == [False, True]


def test_refresh_without_cookie_is_401(client: TestClient) -> None:
    response = client.post(REFRESH)

    assert response.status_code == 401


def test_refresh_with_unknown_token_is_401(client: TestClient) -> None:
    response = _post_with_refresh_cookie(client, REFRESH, "made-up-token")

    assert response.status_code == 401


def test_reusing_a_rotated_token_revokes_the_whole_family(client: TestClient, db: Session) -> None:
    # The real user logs in and refreshes once: T1 -> T2.
    _, token_1 = _registered_and_logged_in(client)
    token_2 = _post_with_refresh_cookie(client, REFRESH, token_1).cookies["refresh_token"]
    _move_revocations_past_grace_period(db)

    # A thief who copied T1 earlier tries to use it.
    with capture_logs() as logs:
        replay = _post_with_refresh_cookie(client, REFRESH, token_1)

    assert replay.status_code == 401
    assert any(event["event"] == "refresh_token_reuse_detected" for event in logs)
    # ...and now even the legitimate T2 no longer works: everyone must log in again.
    assert _post_with_refresh_cookie(client, REFRESH, token_2).status_code == 401
    assert all(row.revoked_at is not None for row in db.scalars(select(RefreshToken)))


def test_reuse_detection_leaves_other_sessions_alone(client: TestClient, db: Session) -> None:
    # Same user, two devices = two separate logins = two families.
    _, laptop_token = _registered_and_logged_in(client)
    phone_token = _login(client).cookies["refresh_token"]

    # Trigger reuse detection on the laptop's family.
    _post_with_refresh_cookie(client, REFRESH, laptop_token)
    _move_revocations_past_grace_period(db)
    with capture_logs() as logs:
        _post_with_refresh_cookie(client, REFRESH, laptop_token)
    assert any(event["event"] == "refresh_token_reuse_detected" for event in logs)

    assert _post_with_refresh_cookie(client, REFRESH, phone_token).status_code == 200


def test_concurrent_refresh_does_not_log_the_user_out(client: TestClient) -> None:
    """Two tabs refresh with the same cookie at the same moment.

    We replay the requests one after the other: that is exactly what the
    second request sees after waiting for the first one's row lock.
    """
    _, token_1 = _registered_and_logged_in(client)

    # Request A wins: T1 -> T2.
    token_2 = _post_with_refresh_cookie(client, REFRESH, token_1).cookies["refresh_token"]
    # Request B arrives with T1 a moment later.
    with capture_logs() as logs:
        late = _post_with_refresh_cookie(client, REFRESH, token_1)

    assert late.status_code == 401  # T1 is used up either way
    assert not any(event["event"] == "refresh_token_reuse_detected" for event in logs)
    # The important part: the session survives, T2 still works.
    assert _post_with_refresh_cookie(client, REFRESH, token_2).status_code == 200


def test_expired_refresh_token_is_401(client: TestClient, db: Session) -> None:
    _, refresh_token = _registered_and_logged_in(client)
    db.execute(update(RefreshToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    db.commit()

    response = _post_with_refresh_cookie(client, REFRESH, refresh_token)

    assert response.status_code == 401


# --- Logout -----------------------------------------------------------------


def test_logout_clears_cookie(client: TestClient) -> None:
    _, refresh_token = _registered_and_logged_in(client)

    response = _post_with_refresh_cookie(client, LOGOUT, refresh_token)

    assert response.status_code == 204
    # Deleting a cookie = setting it again, already expired.
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith("refresh_token=")
    assert "max-age=0" in cookie


def test_logout_without_cookie_still_succeeds(client: TestClient) -> None:
    assert client.post(LOGOUT).status_code == 204


def test_refresh_after_logout_is_401(client: TestClient, db: Session) -> None:
    """A logged-out refresh token can never be used again."""
    _, refresh_token = _registered_and_logged_in(client)
    # logout with that refresh token
    response = _post_with_refresh_cookie(client, LOGOUT, refresh_token)
    assert response.status_code == 204
    # try to refresh with the same token
    response = _post_with_refresh_cookie(client, REFRESH, refresh_token)
    assert response.status_code == 401
    # assert that every RefreshToken row has revoked_at set
    rows = db.scalars(select(RefreshToken)).all()
    # Check the count first: all() on an empty list is True, so without this
    # the next line would also pass if logout DELETED the rows.
    assert len(rows) == 1  # one login = one token; logout revokes it, doesn't delete it
    assert all(row.revoked_at is not None for row in rows)
