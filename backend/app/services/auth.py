"""Auth business logic: register, login, refresh (with rotation), logout.

Routes in app/api/v1/auth.py only translate HTTP <-> these functions. Nothing
here knows about cookies or headers; the functions take plain values and
return plain values, which keeps them easy to test and to explain.

Refresh token lifecycle (full reasoning in docs/decisions/005-auth-tokens.md):

    login    -> new family F, token T1 stored (hash only)
    refresh  -> T1 revoked, T2 issued in family F      ("rotation")
    refresh  -> T2 revoked, T3 issued in family F
    T1 again, within 10 s of its rotation
             -> refused, family untouched (two refreshes at once: a race)
    T1 again, later
             -> T1 is already revoked = someone replayed an old token
             -> revoke EVERY token in family F ("reuse detection")
    logout   -> every token in family F revoked
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, UnauthorizedError
from app.core.security import (
    REFRESH_TOKEN_TTL,
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    password_needs_rehash,
    verify_password,
)
from app.db.errors import violates_constraint
from app.models import RefreshToken, User

log = structlog.get_logger()

# One message for every login failure (SPEC §7.1). Saying "no such email"
# would let anyone check which emails have an account here.
INVALID_LOGIN_MESSAGE = "Invalid email or password."
INVALID_REFRESH_MESSAGE = "Invalid or expired refresh token."

# A token revoked less than this long ago is treated as a harmless race
# (two refreshes at once), not as theft. See the grace branch in refresh()
# and ADR 005. Short, because it is also a window in which a replayed
# stolen token is refused without raising the alarm.
REUSE_GRACE_PERIOD = timedelta(seconds=10)

# Name given by our naming convention (app/db/base.py), see migration 0002.
USERS_EMAIL_CONSTRAINT = "uq_users_email"


@dataclass(frozen=True)
class AuthTokens:
    """The two tokens handed out by login and refresh.

    The route puts `access_token` in the JSON body and `refresh_token` in
    the httpOnly cookie.
    """

    access_token: str
    refresh_token: str


# --- Register ---------------------------------------------------------------


def register_user(db: Session, *, email: str, password: str, name: str) -> User:
    """Create a user. Raises ConflictError (409) if the email is taken.

    We don't check "does this email exist?" first and then insert: two
    requests at the same moment could both pass the check. Instead we just
    insert and let the database's unique constraint on `email` decide.
    CITEXT makes that check case-insensitive.
    """
    user = User(email=email, password_hash=hash_password(password), name=name)
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # Only the email constraint means "taken"; any other integrity
        # error is unexpected and stays a 500 (see violates_constraint).
        if violates_constraint(exc, USERS_EMAIL_CONSTRAINT):
            raise ConflictError("An account with this email already exists.") from None
        raise

    log.info("user_registered", user_id=str(user.id))
    return user


# --- Login ------------------------------------------------------------------


@cache
def _dummy_password_hash() -> str:
    # Built once, on first use (hashing takes ~50 ms, so not at import time).
    return hash_password("dummy-password-for-timing")


def login(db: Session, *, email: str, password: str) -> AuthTokens:
    """Check the credentials and start a new session (a new token family)."""
    user = db.scalar(select(User).where(User.email == email))

    if user is None:
        # Timing attack protection: checking a password takes ~50 ms, but
        # "no such user" would return instantly. An attacker could time the
        # responses to find out which emails are registered. So we do the
        # same slow work against a dummy hash and throw the result away.
        verify_password(password, _dummy_password_hash())
        log.info("login_failed")
        raise UnauthorizedError(INVALID_LOGIN_MESSAGE)

    if not verify_password(password, user.password_hash):
        log.info("login_failed", user_id=str(user.id))
        raise UnauthorizedError(INVALID_LOGIN_MESSAGE)

    # The only moment we hold the plain password: upgrade an old hash if we
    # have raised the argon2 cost parameters since it was made.
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    refresh_token = _issue_refresh_token(db, user_id=user.id, family_id=uuid.uuid4())
    db.commit()

    log.info("login_succeeded", user_id=str(user.id))
    return AuthTokens(access_token=create_access_token(user.id), refresh_token=refresh_token)


# --- Refresh ----------------------------------------------------------------


def refresh(db: Session, *, refresh_token: str) -> AuthTokens:
    """Swap a valid refresh token for a new access token AND a new refresh token."""
    # with_for_update() -> SELECT ... FOR UPDATE: locks this row until we
    # commit. If two requests send the same token at the same moment, the
    # second one waits, then sees `revoked_at` already set by the first and
    # lands in the grace-window branch below.
    # Without the lock, both could read "not revoked" and both get new tokens.
    stored = db.scalar(
        select(RefreshToken)
        .where(RefreshToken.token_hash == hash_refresh_token(refresh_token))
        .with_for_update()
    )
    # Read the clock AFTER the lock: a request that waited for it must
    # compare against the current time, not the time it arrived.
    now = datetime.now(UTC)

    if stored is None:
        # Unknown token: made up, or its user was deleted.
        raise UnauthorizedError(INVALID_REFRESH_MESSAGE)

    if stored.revoked_at is not None and now - stored.revoked_at < REUSE_GRACE_PERIOD:
        # Revoked moments ago: almost certainly the same browser sending two
        # refreshes at once (two tabs, or several API calls retrying after
        # the access token expired). The other request already rotated the
        # token and the browser now holds the new one, so we just refuse
        # this one and do NOT revoke the family.
        #
        # Why this can't hide a logout: logout and reuse detection revoke
        # the whole family, so there is nothing left to protect. The only
        # tokens this branch spares are ones just replaced by rotation.
        log.info("refresh_token_concurrent_use", user_id=str(stored.user_id))
        raise UnauthorizedError(INVALID_REFRESH_MESSAGE)

    if stored.revoked_at is not None:
        # REUSE DETECTED. A token that was already rotated is being used
        # again, so two parties hold tokens from this family: the real user
        # and a thief. We can't tell which request is which, so we log
        # everyone in this family out. The real user just logs in again;
        # the thief loses access.
        _revoke_family(db, family_id=stored.family_id, now=now)
        db.commit()
        log.warning(
            "refresh_token_reuse_detected",
            user_id=str(stored.user_id),
            family_id=str(stored.family_id),
        )
        raise UnauthorizedError(INVALID_REFRESH_MESSAGE)

    if stored.expires_at <= now:
        raise UnauthorizedError(INVALID_REFRESH_MESSAGE)

    # Rotation: this token is now used up; a fresh one continues the family.
    stored.revoked_at = now
    new_refresh_token = _issue_refresh_token(db, user_id=stored.user_id, family_id=stored.family_id)
    db.commit()

    log.info("token_refreshed", user_id=str(stored.user_id))
    return AuthTokens(
        access_token=create_access_token(stored.user_id), refresh_token=new_refresh_token
    )


# --- Logout -----------------------------------------------------------------


def logout(db: Session, *, refresh_token: str | None) -> None:
    """End this session by revoking its whole token family.

    Always succeeds, even for a missing or unknown token: logging out twice
    (or with an already-expired session) should not show an error.
    Other sessions of the same user (other devices = other families) stay
    logged in.
    """
    if refresh_token is None:
        return

    stored = db.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(refresh_token))
    )
    if stored is None:
        return

    _revoke_family(db, family_id=stored.family_id, now=datetime.now(UTC))
    db.commit()
    log.info("logged_out", user_id=str(stored.user_id))


# --- Helpers ----------------------------------------------------------------


def _issue_refresh_token(db: Session, *, user_id: uuid.UUID, family_id: uuid.UUID) -> str:
    """Store the HASH of a new refresh token and return the raw token.

    The raw token leaves this function only to go into the user's cookie;
    it is never stored or logged. The caller commits.
    """
    token = generate_refresh_token()
    db.add(
        RefreshToken(
            user_id=user_id,
            token_hash=hash_refresh_token(token),
            family_id=family_id,
            expires_at=datetime.now(UTC) + REFRESH_TOKEN_TTL,
        )
    )
    return token


def _revoke_family(db: Session, *, family_id: uuid.UUID, now: datetime) -> None:
    """Revoke every still-active token of one family, in a single UPDATE.

    `revoked_at IS NULL` keeps the original revoke time on tokens that were
    already revoked (useful when investigating a theft).
    """
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
