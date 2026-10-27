"""Request and response models for the /transactions endpoints.

Three kinds of model, one for each direction of traffic:

- `TransactionFilters`  the query string of `GET /transactions`
- `TransactionUpdate`   the JSON body of `PATCH /transactions/{id}`
- `TransactionResponse` / `TransactionPage`  what both return

`TransactionFilters` is a *query parameter model*: FastAPI fills it from
the query string, so `?from=2026-08-01&limit=20` is validated by Pydantic
exactly like a JSON body would be. That is what lets the cross-field
checks ("from" not later than "to") live here as plain Python instead of
as `if` statements in the route.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)

from app.models import CategorySource, Channel
from app.parsers.base import MAX_PAISE, Direction
from app.schemas.common import NO_CONTROL_CHARS, QueryFilters
from app.utils.cursor import CursorError, CursorKey, decode_cursor
from app.utils.money import format_inr

# How many transactions one page holds. 50 fills a screen; the ceiling
# keeps one request from pulling a whole year into memory.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# The `q=` search text. Searching for a 1000-character string is never
# useful and it would be compared against every narration, so it is capped.
SearchText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=100, pattern=NO_CONTROL_CHARS),
]

# A note the user writes on a transaction ("gift for Mom"). One line
# (NO_CONTROL_CHARS rejects newlines and NUL), and bounded because it is
# read back on every page of results.
Notes = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=500, pattern=NO_CONTROL_CHARS),
]

# `min_amount` / `max_amount` are given in RUPEES, because that is what the
# user sees and types; they are converted to paise below. The upper bound
# is the largest amount that can be stored at all (`MAX_PAISE`), so an
# absurd filter is a clean 422. Decimal, never float, and at most 2 decimal
# places (CLAUDE.md rule 1): a query parameter arrives as a STRING, so
# Decimal("500.50") is exact.
MAX_AMOUNT_RUPEES = Decimal(MAX_PAISE // 100)
AmountInRupees = Annotated[Decimal, Field(ge=0, le=MAX_AMOUNT_RUPEES, decimal_places=2)]

# `category_confidence` is a NUMERIC(3,2) column, so the ORM hands us a
# Decimal - and Pydantic's default JSON encoding for a Decimal is a quoted
# STRING ("0.95"), which would make a field SPEC §5 calls numeric arrive as
# text and compare wrongly in JavaScript. It is serialized as a number
# instead. Decimal is kept on the way IN (exactness costs nothing here);
# only the output is a float, which is safe because this is a 0.00-1.00
# confidence, not money (rule 1 is about money).
Confidence = Annotated[Decimal, PlainSerializer(float, return_type=float, when_used="unless-none")]

# A cursor is about 64 characters (see app/utils/cursor.py). The cap is
# only there so a megabyte of base64 is rejected before being decoded.
MAX_CURSOR_LENGTH = 200


def _to_paise(rupees: Decimal | None) -> int | None:
    """Rupees as a Decimal -> integer paise (rule 1: never through a float)."""
    return None if rupees is None else int(rupees * 100)


class TransactionFilters(QueryFilters):
    """The query string of `GET /transactions` (SPEC §10).

    From `QueryFilters`: an unknown parameter is a 422 rather than a
    filter that is silently ignored, and a blank value (`?from=`) means
    "not sent" rather than a validation error.
    """

    # `from` is a Python keyword, so the field is named date_from and
    # exposed under its SPEC name with an alias.
    date_from: date | None = Field(
        default=None, alias="from", description="Only transactions on or after this date"
    )
    date_to: date | None = Field(
        default=None, alias="to", description="Only transactions on or before this date"
    )
    account_id: uuid.UUID | None = Field(default=None, description="Only one of your accounts")
    category_id: uuid.UUID | None = Field(
        default=None, description="Only transactions in this category"
    )
    direction: Direction | None = Field(default=None, description="debit = money out, credit = in")
    q: SearchText | None = Field(
        default=None, description="Free text search in the cleaned-up narration"
    )
    min_amount: AmountInRupees | None = Field(
        default=None, description="Smallest amount to include, in rupees (e.g. 500.50)"
    )
    max_amount: AmountInRupees | None = Field(
        default=None, description="Largest amount to include, in rupees"
    )
    cursor: str | None = Field(
        default=None,
        max_length=MAX_CURSOR_LENGTH,
        description="`next_cursor` from the previous page; leave empty for the first page",
    )
    limit: int = Field(
        default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Rows per page (max 200)"
    )

    @field_validator("cursor")
    @classmethod
    def _cursor_must_be_ours(cls, value: str | None) -> str | None:
        """Reject a cursor we did not issue, naming the field (422).

        Only checked here; `cursor_key()` decodes it again when the query
        is built. Decoding is pure and cheap, and this way the service
        never has to deal with a broken cursor.
        """
        # Stripped first, so `?cursor=` (and a cursor a copy/paste padded
        # with spaces) means "the first page" rather than a broken cursor.
        cursor = (value or "").strip()
        if not cursor:
            return None
        try:
            decode_cursor(cursor)
        except CursorError as exc:
            # A ValueError raised in a validator becomes our standard 422.
            raise ValueError(str(exc)) from None
        return cursor

    @model_validator(mode="after")
    def _ranges_must_not_be_backwards(self) -> "TransactionFilters":
        """Catch a reversed range instead of answering with an empty list.

        `?from=2026-09-01&to=2026-08-01` can only be a mistake (a swapped
        pair of date pickers, usually). Returning "no transactions" would
        look like an answer; a 422 says what is actually wrong.
        """
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("'from' must not be later than 'to'")
        if (
            self.min_amount is not None
            and self.max_amount is not None
            and self.min_amount > self.max_amount
        ):
            raise ValueError("'min_amount' must not be greater than 'max_amount'")
        return self

    @property
    def min_amount_paise(self) -> int | None:
        return _to_paise(self.min_amount)

    @property
    def max_amount_paise(self) -> int | None:
        return _to_paise(self.max_amount)

    def cursor_key(self) -> CursorKey | None:
        """The `(txn_date, statement_seq, id)` position to continue from, if any."""
        return None if self.cursor is None else decode_cursor(self.cursor)


class TransactionUpdate(BaseModel):
    """Body of `PATCH /transactions/{id}`: manual categorization (Step 8).

    Unlike `AccountUpdate`, an explicit null is ALLOWED here and means
    something: `{"category_id": null}` makes the transaction uncategorized
    again, and `{"notes": null}` removes the note. "Field not sent" and
    "field set to null" are therefore different requests, which the
    service tells apart with `model_dump(exclude_unset=True)`.

    `apply_to_similar=true` (Phase 2 Step 7, SPEC §8.1) also learns a rule
    from the correction, so every similar row - past and future - is filed
    the same way (see `update_transaction` and app/services/rules.py).

    `is_self_transfer` is in SPEC §10 but is not accepted here, so sending
    it is a 422 rather than a field that quietly does nothing. It is not a
    free-standing switch: it is derived from the category. Choosing a
    category of kind `transfer` sets it, any other category clears it (see
    `_set_category` in app/services/transactions.py). That keeps the flag
    the insights read in step with the category the user can see, instead
    of offering two controls that can contradict each other.
    """

    model_config = ConfigDict(extra="forbid")

    category_id: uuid.UUID | None = Field(
        default=None, description="A system category or one of your own; null clears it"
    )
    notes: Notes | None = Field(default=None, description="Your own note; null or empty clears it")
    apply_to_similar: bool = Field(
        default=False,
        description=(
            "Also file every similar transaction (same person's UPI id, or same merchant) "
            "under this category, now and in future imports"
        ),
    )

    @model_validator(mode="after")
    def _similar_needs_a_category(self) -> "TransactionUpdate":
        # "File everything like this under... nothing" is not a rule. A
        # model validator because it reads two fields; `category_id` must
        # have been SENT, and not as null.
        if self.apply_to_similar and self.category_id is None:
            raise ValueError("apply_to_similar needs a category_id")
        return self


class TransactionResponse(BaseModel):
    """One stored transaction, as the API shows it.

    Money is an integer number of paise (rule 1). `amount_display` is the
    same number formatted for Indian readers (rule 10) - derived, read
    only, and never parsed back: `amount_paise` is the canonical value.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    # Which import created this row, so the user can tell where it came from.
    upload_id: uuid.UUID
    txn_date: date
    value_date: date | None
    amount_paise: int
    direction: Direction
    balance_after_paise: int | None
    raw_description: str
    normalized_description: str
    # Always "other" / null until Phase 2 detects them (SPEC §6.5).
    channel: Channel
    counterparty_vpa: str | None
    category_id: uuid.UUID | None
    category_source: CategorySource
    category_confidence: Confidence | None
    is_self_transfer: bool
    notes: str | None
    created_at: datetime

    # A computed field is part of the response (and of /docs) but never of
    # the input. Always positive: `direction` carries the sign (ADR 001).
    @computed_field(description='The amount in Indian format, e.g. "₹1,25,000.00"')
    @property
    def amount_display(self) -> str:
        return format_inr(self.amount_paise)


class TransactionPage(BaseModel):
    """One page of `GET /transactions`.

    `next_cursor` is null on the last page. A client pages by passing it
    back as `?cursor=...` until it is null - it never has to count rows or
    guess how many pages there are.
    """

    items: list[TransactionResponse]
    next_cursor: str | None = None
