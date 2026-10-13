"""Request and response models for the /insights endpoints (SPEC §9.1).

Two endpoints, so two query models and two response models:

- `SummaryQuery`            -> `SummaryResponse`             (one month)
- `CategoryBreakdownQuery`  -> `CategoryBreakdownResponse`   (a date range)

Both query models inherit `QueryFilters`, so an unknown parameter is a 422
and a blank one (`?month=`) means "not sent" - the same rules
`GET /transactions` follows.

Money appears twice in every response: `*_paise` is the canonical integer
(rule 1) and `*_display` is the same number formatted for Indian readers,
e.g. "₹1,25,000.00" (rule 10). The display string is derived and read-only;
nothing ever parses it back.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    computed_field,
    model_validator,
)

from app.models import CategoryKind
from app.schemas.common import QueryFilters
from app.utils.dates import MONTH_PATTERN, Month
from app.utils.money import format_inr

# Percentages (a change vs last month, a category's share of spending) are
# computed as Decimals so the rounding is exact, then serialized as JSON
# NUMBERS. Pydantic's default for a Decimal is a quoted string ("12.5"),
# which a frontend would have to parse before it could draw a bar.
# A percentage is not money, so a float on the way out is safe (rule 1).
Percent = Annotated[Decimal, PlainSerializer(float, return_type=float, when_used="unless-none")]


# --- Queries --------------------------------------------------------------


class SummaryQuery(QueryFilters):
    """The query string of `GET /insights/summary`.

    `month` is optional: with no month, the summary is for the current
    month *in India* (CLAUDE.md rule 10 - see app/utils/dates.py for why
    the server's own clock is not good enough).
    """

    month: str | None = Field(
        default=None,
        pattern=MONTH_PATTERN,
        description="The month to summarize, as YYYY-MM. Defaults to the current month (IST).",
        examples=["2026-08"],
    )

    @model_validator(mode="after")
    def _month_must_have_a_previous_month(self) -> Self:
        """Reject a month we could not compare against (422, not a 500).

        The response includes the month before the requested one, so a
        month at the very start of the supported range has no comparison
        and `Month.previous()` would raise - here, inside a validator,
        that is a clean 422 naming `month`; from inside the service it
        would be an unhandled 500.

        The pattern on the field has already rejected "2026-13" and
        "August 2026", so what is left to catch is the year bound
        (`Month` allows 1900-2999, see app/utils/dates.py).
        """
        if self.month is not None:
            # A ValueError raised in a validator becomes our standard 422.
            Month.parse(self.month).previous()
        return self

    def resolved_month(self) -> Month:
        """The month the request is about: the one given, or this month."""
        return Month.current() if self.month is None else Month.parse(self.month)


class CategoryBreakdownQuery(QueryFilters):
    """The query string of `GET /insights/categories`.

    `from` and `to` are **both or neither**: with neither, the range is the
    current month. Defaulting only the missing half was the alternative and
    it reads worse in practice - `?to=2026-08-31` would silently mean "from
    the 1st of *this* month", which for any `to` in the past is an empty
    range, and the user would see "no spending" instead of being told the
    request did not say what they thought it said.
    """

    date_from: date | None = Field(
        default=None, alias="from", description="First day of the range (inclusive)"
    )
    date_to: date | None = Field(
        default=None, alias="to", description="Last day of the range (inclusive)"
    )

    @model_validator(mode="after")
    def _range_must_be_complete_and_forwards(self) -> Self:
        if (self.date_from is None) != (self.date_to is None):
            raise ValueError("give both 'from' and 'to', or neither")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("'from' must not be later than 'to'")
        return self

    def resolved_range(self) -> tuple[date, date]:
        """The range the request is about: the one given, or this month."""
        if self.date_from is None or self.date_to is None:
            month = Month.current()
            return month.first_day, month.last_day
        return self.date_from, self.date_to


# --- Responses ------------------------------------------------------------


class PeriodTotals(BaseModel):
    """Income, spending and the balance between them, over one period.

    `net_paise` is `income - spend` and is the only money value in the API
    that can be negative: a month where more went out than came in. The
    formatter prints that as "-₹2,500.00".

    Self transfers are excluded from all three (SPEC §9.1): moving ₹10,000
    from savings to a credit card is not income and not spending, and
    counting it as both would inflate the month at both ends.
    """

    month: str = Field(description="The month these totals cover, as YYYY-MM")
    income_paise: int = Field(description="Money in (credits), in paise")
    spend_paise: int = Field(description="Money out (debits), in paise")
    net_paise: int = Field(description="income - spend, in paise; negative if you overspent")
    transaction_count: int = Field(description="How many transactions are behind these totals")

    @computed_field(description='Income in Indian format, e.g. "₹50,000.00"')
    @property
    def income_display(self) -> str:
        return format_inr(self.income_paise)

    @computed_field(description="Spending in Indian format")
    @property
    def spend_display(self) -> str:
        return format_inr(self.spend_paise)

    @computed_field(description="Net in Indian format; starts with '-' in a month you overspent")
    @property
    def net_display(self) -> str:
        return format_inr(self.net_paise)


class SummaryResponse(BaseModel):
    """`GET /insights/summary`: one month, next to the month before it.

    The two change percentages are `null` when the previous month has
    nothing to compare against (its total was zero). That is deliberate:
    "no basis for comparison" and "no change" are different facts, and
    sending 0 for the first would draw a flat line where there is no line.
    An increase from nothing is not "+100%", it is unmeasurable.

    There is no percentage for `net`, because a change in a value that can
    cross zero does not mean anything: going from -₹500 to +₹500 is not a
    "-200% change" in any sense a reader would accept.
    """

    # The JSON uses "from"/"to" (SPEC §10's own parameter names), but
    # `from` cannot be a Python attribute name, so the fields carry
    # aliases: `populate_by_name` lets our code build the model with the
    # Python names, `serialize_by_alias` makes the response use the JSON
    # ones.
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    month: str = Field(description="The month summarized, as YYYY-MM")
    date_from: date = Field(alias="from", description="First day of the month")
    date_to: date = Field(alias="to", description="Last day of the month")
    current: PeriodTotals
    previous: PeriodTotals
    income_change_pct: Percent | None = Field(
        default=None, description="Income vs the previous month, in %; null if that month was zero"
    )
    spend_change_pct: Percent | None = Field(
        default=None, description="Spending vs the previous month, in %; null if it was zero"
    )


class CategorySpend(BaseModel):
    """What one category cost over the range.

    `category_id` is `null` for the "not categorized yet" bucket - rows the
    user has not filed and the Phase 2 categorizer has not reached. It is
    kept as a row of its own rather than dropped, so the parts always add
    up to `total_spend_paise`; in Phase 1 it is usually the biggest row.

    (The seed also contains a system category *named* "Uncategorized",
    which SPEC §8 gives the categorizer as an explicit "I looked and found
    nothing" answer. A row filed there has a real `category_id`, so the two
    appear separately - a deliberate distinction between "not looked at"
    and "looked at, nothing fits".)
    """

    category_id: uuid.UUID | None = Field(description="null = not categorized yet")
    name: str
    slug: str | None = Field(description="null for the uncategorized bucket")
    kind: CategoryKind | None = Field(description="null for the uncategorized bucket")
    icon: str | None
    color: str | None
    amount_paise: int
    transaction_count: int
    share_pct: Percent = Field(description="This category's share of total spending, in %")

    @computed_field(description='The amount in Indian format, e.g. "₹1,320.50"')
    @property
    def amount_display(self) -> str:
        return format_inr(self.amount_paise)


class CategoryBreakdownResponse(BaseModel):
    """`GET /insights/categories`: spending per category over a date range.

    Debits only, self transfers excluded (SPEC §9.1). Income does not
    appear here at all - a "Salary" category has no spending, so the
    biggest number on the screen would otherwise be money coming in.

    Largest first, so the answer to "where did it go?" is the first row.
    """

    # Same alias handling as SummaryResponse above.
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    date_from: date = Field(alias="from")
    date_to: date = Field(alias="to")
    total_spend_paise: int = Field(description="The sum of every item below, in paise")
    items: list[CategorySpend]

    @computed_field(description="Total spending in Indian format")
    @property
    def total_spend_display(self) -> str:
        return format_inr(self.total_spend_paise)
