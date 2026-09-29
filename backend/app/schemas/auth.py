"""Request and response models for the /auth endpoints.

Pydantic validates every incoming body against these classes before our code
runs. A body that doesn't match (bad email, short password, missing field)
is rejected with a 422 VALIDATION_ERROR, and the route is never called.
"""

import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

# SPEC §7.1: minimum length 8. The maximum stops someone from sending a 10 MB
# "password" that we would then have to hash. 128 is far above any real
# password, including long passphrases from a password manager.
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128

# `Annotated[str, StringConstraints(...)]` is "a str with extra rules".
# strip_whitespace removes spaces around the name before the length check,
# so "   " counts as empty and is rejected.
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Password = Annotated[str, Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)]


class RegisterRequest(BaseModel):
    # extra="forbid": unknown fields are an error instead of being silently
    # ignored, so a typo like "pasword" is caught.
    model_config = ConfigDict(extra="forbid")

    # EmailStr checks the format with the `email-validator` package.
    email: EmailStr
    password: Password
    name: Name


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    # Only max_length here, no min_length: a login attempt with a short
    # password should get the normal "invalid email or password" answer, not
    # a validation error that hints at our password rules.
    password: Annotated[str, Field(max_length=MAX_PASSWORD_LENGTH)]


class UserResponse(BaseModel):
    """What the API shows about a user. Never the password hash or settings."""

    # from_attributes=True lets us build this from an ORM object:
    # UserResponse.model_validate(user) reads user.id, user.email, user.name.
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    name: str


class TokenResponse(BaseModel):
    """Body returned by login and refresh.

    The refresh token is NOT in here: it travels only in the httpOnly cookie,
    where JavaScript (and so an XSS attack) can't read it.
    """

    access_token: str
    # Tells the client how to send it: `Authorization: Bearer <access_token>`.
    # noqa S105: ruff sees "token" in the name; "bearer" is a label, not a secret.
    token_type: Literal["bearer"] = "bearer"  # noqa: S105
    # Seconds until the access token expires, so the frontend knows when to
    # call /auth/refresh without decoding the JWT itself.
    expires_in: int
