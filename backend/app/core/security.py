"""Password hashing and login tokens.

Three independent tools live here:

1. Passwords   : hashed with argon2id. We never store or log the password
                 itself, only the hash, and a hash can't be turned back into
                 the password.
2. Access token: a short-lived (15 min) JWT sent by the client in the
                 `Authorization: Bearer <token>` header on every request.
                 It is *stateless*: the server checks its signature and
                 expiry without touching the database.
3. Refresh token: a long-lived random string, kept in an httpOnly cookie and
                 used only to get a new access token. It is *stateful*: the
                 database stores a hash of it, so it can be revoked (logout,
                 theft detection). See ADR 005 (Step 2) for the full design.

This module is pure helper code: no database, no FastAPI. The auth service
(Step 2) combines these helpers with the `users` and `refresh_tokens` tables.
"""

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import get_settings
from app.core.errors import UnauthorizedError

ACCESS_TOKEN_TTL = timedelta(minutes=15)
# Not fixed by the spec. 30 days means "stay logged in for a month of
# inactivity"; every refresh issues a new token, so active users never expire.
REFRESH_TOKEN_TTL = timedelta(days=30)

# HS256 = HMAC-SHA256: the same secret signs and verifies the token. Fine for
# us because only this one API creates and checks tokens.
JWT_ALGORITHM = "HS256"
# noqa S105: ruff's "hardcoded password" check fires on the word TOKEN in
# the name. This is a label written into the token, not a secret.
ACCESS_TOKEN_TYPE = "access"  # noqa: S105

# 32 random bytes = 256 bits: far too many possibilities to guess.
REFRESH_TOKEN_BYTES = 32

# --- Passwords --------------------------------------------------------------

# PasswordHasher() defaults to argon2id with the parameters recommended by
# RFC 9106. argon2id is deliberately slow and memory-hungry, so an attacker
# who steals the hashes can only try a few guesses per second per CPU core.
# Each hash also contains its own random salt and its parameters, e.g.
#   $argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>
_password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """True if `password` matches the stored hash, otherwise False.

    argon2 signals "no match" by raising an exception; we turn every failure
    (wrong password, or a corrupted hash in the DB) into a plain False so
    callers can't forget to catch it.
    """
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """True if the hash was made with older/weaker argon2 parameters.

    Used right after a successful login: if we ever raise the cost
    parameters, users' hashes are upgraded the next time they log in
    (the only moment we have their plain password).
    """
    return _password_hasher.check_needs_rehash(password_hash)


# --- Access tokens (JWT) ----------------------------------------------------


def create_access_token(user_id: uuid.UUID, *, now: datetime | None = None) -> str:
    """Create a signed JWT that says "this is user <user_id>" for 15 minutes.

    `now` exists only so tests can create tokens "in the past" (e.g. an
    already-expired one) without waiting 15 minutes.
    """
    issued_at = now or datetime.now(UTC)
    payload = {
        "sub": str(user_id),  # "subject": who the token is about
        "type": ACCESS_TOKEN_TYPE,
        "iat": issued_at,  # "issued at"
        "exp": issued_at + ACCESS_TOKEN_TTL,  # "expires"; PyJWT checks it on decode
    }
    secret = get_settings().jwt_access_secret.get_secret_value()
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> uuid.UUID:
    """Verify an access token and return the user id inside it.

    Raises UnauthorizedError for anything wrong: bad signature, expired,
    malformed, missing claims, or not an access token. The caller doesn't
    need to know which, and neither does the client (one generic message).
    """
    secret = get_settings().jwt_access_secret.get_secret_value()
    try:
        payload = jwt.decode(
            token,
            secret,
            # Always pass the allowed algorithms explicitly. Otherwise an
            # attacker could send a token with "alg": "none" (no signature)
            # and some libraries would accept it: the classic JWT attack.
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "exp", "iat"]},
        )
        if payload.get("type") != ACCESS_TOKEN_TYPE:
            raise UnauthorizedError("Invalid or expired token.")
        return uuid.UUID(payload["sub"])
    except (jwt.InvalidTokenError, ValueError):
        # `from None`: don't chain PyJWT's error, which describes the token.
        raise UnauthorizedError("Invalid or expired token.") from None


# --- Refresh tokens -----------------------------------------------------------


def generate_refresh_token() -> str:
    """A new random refresh token, e.g. 'k3J9...'. Sent to the browser once.

    `secrets` (not `random`) uses the operating system's cryptographically
    secure random generator, so tokens can't be predicted.
    """
    return secrets.token_urlsafe(REFRESH_TOKEN_BYTES)


def hash_refresh_token(token: str) -> str:
    """The value we store in `refresh_tokens.token_hash`.

    Why hash at all? If the database leaked, raw tokens would let an attacker
    log in as every user. A hash can't be sent back to us as a token.

    Why HMAC with a secret, not plain SHA-256? The secret key lives in the
    app's environment, not the database, so a leaked DB alone isn't enough
    to check guesses against the stored hashes.

    Why not argon2 like passwords? The token is 256 random bits, so it
    can't be brute-forced anyway; a fast hash is enough. It also has to be
    deterministic, so we can look a token up by its hash in one query.
    """
    key = get_settings().jwt_refresh_secret.get_secret_value().encode()
    return hmac.new(key, token.encode(), hashlib.sha256).hexdigest()
