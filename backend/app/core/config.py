"""Application settings, loaded from environment variables (and `.env` locally).

Every setting is declared once, with its type, in the `Settings` class.
pydantic-settings reads the matching environment variable (field
`database_url` <- env var `DATABASE_URL`), converts it to the declared type
and runs the validators below. If anything is missing or invalid, the app
refuses to start ("fail fast") instead of crashing later in the middle of a
request.

Usage everywhere else in the app:

    from app.core.config import get_settings
    settings = get_settings()
"""

import base64
import binascii
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py -> core -> app -> backend -> repo root, where `.env` lives.
ENV_FILE = Path(__file__).resolve().parents[3] / ".env"

MIN_SECRET_LENGTH = 32


class ConfigError(RuntimeError):
    """Raised at startup when the configuration is missing or invalid."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        # `GROQ_API_KEY=` (empty) counts as "not set", so optional values become None.
        env_ignore_empty=True,
        # Ignore unrelated variables in the environment / .env file.
        extra="ignore",
    )

    # --- App -------------------------------------------------------------
    env: Literal["development", "test", "production"] = "development"
    api_port: int = 8000
    frontend_origin: str = "http://localhost:3000"
    max_upload_mb: int = Field(default=10, gt=0, le=50)

    # --- Infrastructure (required: no default) ---------------------------
    database_url: str
    test_database_url: str | None = None
    redis_url: str

    # --- Secrets (required) ----------------------------------------------
    # SecretStr hides the value when printed or logged: it shows '**********'.
    # The real value is only available through .get_secret_value().
    jwt_access_secret: SecretStr
    jwt_refresh_secret: SecretStr
    file_encryption_key: SecretStr

    # --- Optional integrations -------------------------------------------
    # The app must work fully without these (LLM and email are optional).
    groq_api_key: SecretStr | None = None
    groq_model: str | None = None
    resend_api_key: SecretStr | None = None

    # --- Validators ------------------------------------------------------
    # A field_validator runs after the value has been converted to its type.
    # Raising ValueError turns into a clear validation error for that field.

    @field_validator("database_url", "test_database_url")
    @classmethod
    def _must_use_psycopg_driver(cls, value: str | None) -> str | None:
        # A plain "postgresql://" URL makes SQLAlchemy look for psycopg2,
        # which we don't install. We use psycopg v3, so the URL must say so.
        if value is not None and not value.startswith("postgresql+psycopg://"):
            raise ValueError("must start with 'postgresql+psycopg://' (psycopg v3 driver)")
        return value

    @field_validator("redis_url")
    @classmethod
    def _must_be_redis_url(cls, value: str) -> str:
        # rediss:// (two s) is Redis over TLS, used by hosted Redis like Upstash.
        if not value.startswith(("redis://", "rediss://")):
            raise ValueError("must start with 'redis://' or 'rediss://'")
        return value

    @field_validator("frontend_origin")
    @classmethod
    def _must_be_origin(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("must start with 'http://' or 'https://'")
        # CORS compares origins as exact strings, and browsers send the origin
        # without a trailing slash, so we strip it here.
        return value.rstrip("/")

    @field_validator("jwt_access_secret", "jwt_refresh_secret")
    @classmethod
    def _must_be_strong(cls, value: SecretStr) -> SecretStr:
        # Also rejects the "change-me" placeholder from .env.example.
        if len(value.get_secret_value()) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"must be at least {MIN_SECRET_LENGTH} characters; generate one with: "
                'python -c "import secrets; print(secrets.token_urlsafe(64))"'
            )
        return value

    @field_validator("file_encryption_key")
    @classmethod
    def _must_be_32_byte_base64_key(cls, value: SecretStr) -> SecretStr:
        # AES-256 needs exactly 32 bytes of key. We store it base64-encoded
        # because raw random bytes can't be written safely in a text file.
        try:
            raw = base64.b64decode(value.get_secret_value(), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("must be valid base64") from None
        if len(raw) != 32:
            raise ValueError(
                f"must decode to exactly 32 bytes (got {len(raw)}); generate one with: "
                'python -c "import base64, os; print(base64.b64encode(os.urandom(32)).decode())"'
            )
        return value

    @model_validator(mode="after")
    def _secrets_must_differ(self) -> "Settings":
        # A model_validator sees ALL fields at once, so it can compare two of
        # them. If both JWT secrets were equal, a refresh token could be
        # accepted as an access token (and vice versa).
        if self.jwt_access_secret.get_secret_value() == self.jwt_refresh_secret.get_secret_value():
            raise ValueError("JWT_ACCESS_SECRET and JWT_REFRESH_SECRET must be different")
        return self

    # --- Convenience -----------------------------------------------------

    @property
    def file_encryption_key_bytes(self) -> bytes:
        """The decoded 32-byte AES key (already validated above)."""
        return base64.b64decode(self.file_encryption_key.get_secret_value())

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


def load_settings(env_file: Path | None = ENV_FILE) -> Settings:
    """Build Settings, turning validation errors into a short, safe message.

    Pydantic's normal error text includes the *input values*, which here could
    be secrets. So we rebuild the message from field names and reasons only,
    and use `from None` so the original error (with values) isn't chained.
    """
    try:
        # `_env_file` is pydantic-settings' way to choose the .env file at
        # runtime. None means "environment variables only" (used in tests).
        return Settings(_env_file=env_file)
    except ValidationError as exc:
        problems = []
        for error in exc.errors(include_input=False, include_url=False):
            field = ".".join(str(part) for part in error["loc"]).upper() or "SETTINGS"
            problems.append(f"  - {field}: {error['msg']}")
        raise ConfigError("Invalid configuration:\n" + "\n".join(problems)) from None


@lru_cache
def get_settings() -> Settings:
    """Return the app's settings, loading them only once.

    `@lru_cache` remembers the return value, so the environment and .env file
    are read on the first call only; every later call returns the same object.
    """
    return load_settings()
