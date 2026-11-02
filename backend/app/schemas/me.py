"""Request and response models for GET /me and DELETE /me."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.auth import MAX_PASSWORD_LENGTH, UserResponse


class UserSettings(BaseModel):
    """The user's preferences (the `users.settings` JSONB column).

    Spelled out field by field, rather than returned as a loose dict, so the
    API documents exactly which settings exist, and a key that somehow got
    into the column is not passed on to the browser.
    """

    ai_enabled: bool
    weekly_email: bool


class MeResponse(UserResponse):
    """GET /me: the logged-in user's own profile.

    Inherits id, email and name from UserResponse and adds the settings.
    Still never the password hash.
    """

    settings: UserSettings


class DeleteMeRequest(BaseModel):
    """DELETE /me body: the current password, asked for again.

    A stolen access token alone must not be enough to wipe someone's data
    (ADR 021).
    """

    model_config = ConfigDict(extra="forbid")

    # Like LoginRequest: only a max_length. A too-short password gets the
    # normal "not correct" answer, not a validation error.
    password: Annotated[str, Field(max_length=MAX_PASSWORD_LENGTH)]
