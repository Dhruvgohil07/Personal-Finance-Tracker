"""Tests for app.core.security: password hashing, access JWTs, refresh tokens."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from argon2 import PasswordHasher

from app.core.config import get_settings
from app.core.errors import UnauthorizedError
from app.core.security import (
    ACCESS_TOKEN_TTL,
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    password_needs_rehash,
    verify_password,
)

USER_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _access_secret() -> str:
    return get_settings().jwt_access_secret.get_secret_value()


# --- Passwords --------------------------------------------------------------


def test_hash_is_argon2id_and_does_not_contain_the_password() -> None:
    password_hash = hash_password("correct horse battery")

    assert password_hash.startswith("$argon2id$")
    assert "correct horse battery" not in password_hash


def test_same_password_gets_a_different_hash_each_time() -> None:
    # A random salt per hash: two users with the same password don't get the
    # same hash, so one cracked hash doesn't reveal everyone else's.
    assert hash_password("same-password") != hash_password("same-password")


def test_verify_password_accepts_the_right_password_only() -> None:
    password_hash = hash_password("right-password")

    assert verify_password("right-password", password_hash) is True
    assert verify_password("wrong-password", password_hash) is False


def test_verify_password_returns_false_for_a_corrupted_hash() -> None:
    assert verify_password("anything", "not-an-argon2-hash") is False


def test_fresh_hash_does_not_need_rehash() -> None:
    assert password_needs_rehash(hash_password("pw-12345678")) is False


def test_hash_made_with_weaker_parameters_needs_rehash() -> None:
    # Simulates a hash created before we raised the cost parameters.
    weak_hash = PasswordHasher(time_cost=1, memory_cost=8 * 1024).hash("pw-12345678")

    assert password_needs_rehash(weak_hash) is True


# --- Access tokens ------------------------------------------------------------


def test_access_token_round_trip_returns_the_user_id() -> None:
    token = create_access_token(USER_ID)

    assert decode_access_token(token) == USER_ID


def test_access_token_expires_after_15_minutes() -> None:
    issued_at = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
    payload = jwt.decode(
        create_access_token(USER_ID, now=issued_at),
        _access_secret(),
        algorithms=["HS256"],
        options={"verify_exp": False},  # the token is in the past; we only read it
    )

    assert payload["exp"] - payload["iat"] == ACCESS_TOKEN_TTL.total_seconds()


def test_expired_access_token_is_rejected() -> None:
    issued_at = datetime.now(UTC) - timedelta(minutes=16)
    token = create_access_token(USER_ID, now=issued_at)
    with pytest.raises(UnauthorizedError):
        decode_access_token(token)


def test_access_token_still_valid_just_before_expiry() -> None:
    issued_at = datetime.now(UTC) - timedelta(minutes=14)
    token = create_access_token(USER_ID, now=issued_at)

    assert decode_access_token(token) == USER_ID


def test_token_signed_with_another_secret_is_rejected() -> None:
    forged = jwt.encode(
        {"sub": str(USER_ID), "type": "access", "iat": datetime.now(UTC), "exp": 9999999999},
        "an-attacker-secret-that-is-long-enough-123",
        algorithm="HS256",
    )

    with pytest.raises(UnauthorizedError):
        decode_access_token(forged)


def test_unsigned_alg_none_token_is_rejected() -> None:
    # The classic JWT attack: no signature at all, "alg": "none".
    unsigned = jwt.encode(
        {"sub": str(USER_ID), "type": "access", "iat": datetime.now(UTC), "exp": 9999999999},
        key=None,
        algorithm="none",
    )

    with pytest.raises(UnauthorizedError):
        decode_access_token(unsigned)


def test_tampered_token_is_rejected() -> None:
    header, payload, signature = create_access_token(USER_ID).split(".")
    # Change one character of the payload: the signature no longer matches.
    tampered_payload = payload[:-1] + ("A" if payload[-1] != "A" else "B")

    with pytest.raises(UnauthorizedError):
        decode_access_token(f"{header}.{tampered_payload}.{signature}")


@pytest.mark.parametrize(
    "claims",
    [
        {"sub": str(USER_ID), "type": "refresh"},  # wrong token type
        {"sub": str(USER_ID)},  # no type at all
        {"sub": "not-a-uuid", "type": "access"},  # subject isn't a user id
        {"type": "access"},  # no subject
    ],
)
def test_token_with_bad_claims_is_rejected(claims: dict[str, str]) -> None:
    token = jwt.encode(
        {**claims, "iat": datetime.now(UTC), "exp": 9999999999},
        _access_secret(),
        algorithm="HS256",
    )

    with pytest.raises(UnauthorizedError):
        decode_access_token(token)


def test_token_without_expiry_is_rejected() -> None:
    # A token that never expires would be valid forever if stolen.
    token = jwt.encode(
        {"sub": str(USER_ID), "type": "access", "iat": datetime.now(UTC)},
        _access_secret(),
        algorithm="HS256",
    )

    with pytest.raises(UnauthorizedError):
        decode_access_token(token)


def test_garbage_token_is_rejected_with_a_generic_message() -> None:
    with pytest.raises(UnauthorizedError) as exc_info:
        decode_access_token("not.a.jwt")

    assert exc_info.value.message == "Invalid or expired token."


# --- Refresh tokens -----------------------------------------------------------


def test_refresh_tokens_are_random_and_long() -> None:
    tokens = {generate_refresh_token() for _ in range(100)}

    assert len(tokens) == 100  # no repeats
    # 32 random bytes -> 43 URL-safe base64 characters.
    assert all(len(token) >= 43 for token in tokens)


def test_refresh_token_hash_is_deterministic_and_hides_the_token() -> None:
    token = generate_refresh_token()

    # Same input -> same hash, so a token can be looked up by its hash.
    assert hash_refresh_token(token) == hash_refresh_token(token)
    assert token not in hash_refresh_token(token)
    assert len(hash_refresh_token(token)) == 64  # SHA-256 as hex


def test_different_refresh_tokens_have_different_hashes() -> None:
    assert hash_refresh_token("token-one") != hash_refresh_token("token-two")
