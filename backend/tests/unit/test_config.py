"""Tests for app.core.config: valid settings load, invalid ones fail fast."""

import base64

import pytest

from app.core.config import ConfigError, Settings, load_settings

VALID_KEY = base64.b64encode(bytes(range(32))).decode()  # exactly 32 bytes
ACCESS_SECRET = "a" * 40
REFRESH_SECRET = "b" * 40

VALID_ENV = {
    "DATABASE_URL": "postgresql+psycopg://kharcha:kharcha@localhost:5432/kharcha",
    "REDIS_URL": "redis://localhost:6379/0",
    "JWT_ACCESS_SECRET": ACCESS_SECRET,
    "JWT_REFRESH_SECRET": REFRESH_SECRET,
    "FILE_ENCRYPTION_KEY": VALID_KEY,
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with none of our settings in the environment.

    Without this, a DATABASE_URL set on your machine (or in CI) would leak
    into the tests. monkeypatch undoes all changes after each test.
    """
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def valid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in VALID_ENV.items():
        monkeypatch.setenv(name, value)


def load() -> Settings:
    # env_file=None: read environment variables only, never your real .env.
    return load_settings(env_file=None)


def test_valid_settings_load_with_defaults(valid_env: None) -> None:
    settings = load()

    assert settings.env == "development"
    assert settings.max_upload_mb == 10
    assert settings.max_upload_bytes == 10 * 1024 * 1024
    assert settings.file_encryption_key_bytes == bytes(range(32))
    assert settings.groq_api_key is None


def test_empty_optional_value_is_treated_as_unset(
    valid_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "")  # like `GROQ_API_KEY=` in .env

    assert load().groq_api_key is None


def test_frontend_origin_trailing_slash_is_removed(
    valid_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRONTEND_ORIGIN", "http://localhost:3000/")

    assert load().frontend_origin == "http://localhost:3000"


@pytest.mark.parametrize("missing", list(VALID_ENV))
def test_missing_required_setting_fails_fast(
    valid_env: None, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.delenv(missing)

    with pytest.raises(ConfigError, match=missing):
        load()


@pytest.mark.parametrize(
    ("name", "bad_value"),
    [
        ("DATABASE_URL", "postgresql://kharcha:kharcha@localhost/kharcha"),  # psycopg2 driver
        ("REDIS_URL", "localhost:6379"),
        ("FRONTEND_ORIGIN", "localhost:3000"),
        ("ENV", "staging"),
        ("MAX_UPLOAD_MB", "0"),
        ("JWT_ACCESS_SECRET", "change-me"),  # the .env.example placeholder
        ("FILE_ENCRYPTION_KEY", "not base64!!"),
        ("FILE_ENCRYPTION_KEY", base64.b64encode(b"too short").decode()),
    ],
)
def test_invalid_value_fails_fast(
    valid_env: None, monkeypatch: pytest.MonkeyPatch, name: str, bad_value: str
) -> None:
    monkeypatch.setenv(name, bad_value)

    with pytest.raises(ConfigError, match=name):
        load()


def test_jwt_secrets_must_differ(valid_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_REFRESH_SECRET", ACCESS_SECRET)

    with pytest.raises(ConfigError, match="must be different"):
        load()


def test_error_message_never_contains_secret_values(
    valid_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A wrong-length key that is still a real secret must not be echoed back.
    secret_but_wrong_length = base64.b64encode(b"x" * 31).decode()
    monkeypatch.setenv("FILE_ENCRYPTION_KEY", secret_but_wrong_length)
    monkeypatch.setenv("JWT_REFRESH_SECRET", "short-but-secret")

    with pytest.raises(ConfigError) as exc_info:
        load()

    message = str(exc_info.value)
    assert secret_but_wrong_length not in message
    assert "short-but-secret" not in message
    assert exc_info.value.__cause__ is None  # original pydantic error not chained


def test_secrets_are_hidden_when_printed(valid_env: None) -> None:
    settings = load()

    assert ACCESS_SECRET not in repr(settings)
    assert ACCESS_SECRET not in str(settings.jwt_access_secret)
    assert settings.jwt_access_secret.get_secret_value() == ACCESS_SECRET
