"""Request and response models for the /rules endpoints (SPEC §10, ADR 017).

A rule is "rows matching THIS go in THAT category". The one thing worth
reading here is how the pattern is validated: it is normalized for its
match type on the way in (`normalize_rule_pattern`), so what is stored is
exactly what the categorizer compares against, and "Moon Cafe" and
"MOONCAFE" are recognised as the same rule by the unique constraint.
"""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
)

from app.categorizer.handlers import RuleMatchType, normalize_rule_pattern
from app.models.category_rule import DEFAULT_RULE_PRIORITY
from app.schemas.common import NO_CONTROL_CHARS

# Before normalization: trimmed, one line, and short enough to be a name or
# an id rather than a pasted narration.
RawPattern = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=100, pattern=NO_CONTROL_CHARS
    ),
]
# Lower numbers are tried first. Bounded only to keep the numbers human.
Priority = Annotated[int, Field(ge=0, le=1000)]


class RuleCreate(BaseModel):
    """Body of `POST /rules`."""

    model_config = ConfigDict(extra="forbid")

    match_type: RuleMatchType = Field(
        description=(
            "`merchant`: equals a transaction's `merchant_key` (as GET /transactions "
            "shows it; spaces and case do not matter); `vpa`: the other side's UPI id; "
            "`contains`: text anywhere in the narration"
        )
    )
    pattern: RawPattern = Field(description="What to match, e.g. 'Moon Cafe' or 'name@okaxis'")
    category_id: uuid.UUID = Field(description="A system category or one of your own")
    priority: Priority = Field(
        default=DEFAULT_RULE_PRIORITY,
        description="Lower is tried first; ties go to the most recently created or changed",
    )

    # Runs after `match_type` (fields are validated in the order they are
    # declared), so `info.data` holds it. If the match type itself was
    # invalid it is missing, and that 422 is already on its way.
    @field_validator("pattern")
    @classmethod
    def _normalize_pattern(cls, value: str, info: ValidationInfo) -> str:
        match_type = info.data.get("match_type")
        if match_type is None:
            return value
        return normalize_rule_pattern(match_type, value)


class RuleUpdate(BaseModel):
    """Body of `PATCH /rules/{id}`: a new category and/or priority.

    What a rule MATCHES (its type and pattern) cannot be changed: that
    would be a different rule, so delete it and create the new one. The
    same reasoning as `AccountUpdate` refusing a new account number.
    """

    model_config = ConfigDict(extra="forbid")

    category_id: uuid.UUID | None = None
    priority: Priority | None = None

    # `| None` above only means "not sent"; an explicit null would reach a
    # NOT NULL column. Same check as `AccountUpdate._not_null`.
    @field_validator("category_id", "priority")
    @classmethod
    def _not_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("may be left out, but not set to null")
        return value


class RuleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    match_type: RuleMatchType
    pattern: str
    category_id: uuid.UUID
    priority: int
    # The transaction it was learned from (`apply_to_similar`), if any.
    created_from_transaction_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
