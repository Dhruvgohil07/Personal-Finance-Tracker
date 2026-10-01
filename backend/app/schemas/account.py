"""Request and response models for the /accounts endpoints."""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from app.models import AccountType, BankCode
from app.schemas.common import NO_CONTROL_CHARS

# pattern: no control characters such as NUL (see app/schemas/common.py).
Nickname = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=50, pattern=NO_CONTROL_CHARS),
]
# Exactly 4 digits. A user who pastes their full account number gets a 422
# here, before it can reach the database, a log line or an error message
# (the validation handler never echoes the input back, see app/core/errors.py).
MaskedNumber = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}$")]


class AccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Pydantic accepts only the enum's values ("ICICI", "savings", ...) and
    # lists them in /docs.
    bank_code: BankCode
    nickname: Nickname
    account_type: AccountType
    masked_number: MaskedNumber


class AccountUpdate(BaseModel):
    """Body of PATCH /accounts/{id}: only the fields being changed are sent.

    bank_code and masked_number can't be changed: they identify WHICH real
    account this is, and its statements and transactions are tied to that.
    Wrong bank or number = delete the account and add the right one.
    """

    model_config = ConfigDict(extra="forbid")

    # None = "not sent, leave unchanged". To tell "not sent" apart from other
    # values, the service should use `body.model_dump(exclude_unset=True)`.
    nickname: Nickname | None = None
    account_type: AccountType | None = None

    # `| None` above only means "not sent". Without this check a client could
    # also SEND null ({"nickname": null}), which would reach the NOT NULL
    # column and fail as a 500. Pydantic doesn't run validators on default
    # values, so this runs only for fields that were actually in the body.
    @field_validator("nickname", "account_type")
    @classmethod
    def _not_null(cls, value: object) -> object:
        if value is None:
            # A ValueError becomes a normal 422 VALIDATION_ERROR.
            raise ValueError("may be left out, but not set to null")
        return value


class AccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    bank_code: BankCode
    nickname: str
    account_type: AccountType
    masked_number: str
    created_at: datetime
