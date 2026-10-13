"""GET /api/v1/insights/summary and /api/v1/insights/categories (SPEC §9.1).

The two endpoints that turn a pile of imported rows into the two numbers a
person actually wants: "what did this month do to my balance?" and "where
did it go?".

Both routes are deliberately thin (rule 6): read the query model, call the
service, map its dataclasses onto the response models. All the SQL is in
app/services/insights.py and all the validation is in
app/schemas/insights.py.

SPEC §10 lists two more insights endpoints - `/insights/trend?months=12`
and `/insights/top-merchants` - which are not in Phase 1. The trend is the
same monthly aggregation over twelve months instead of two, and top
merchants needs `transactions.merchant_id` to be filled, which is the
Phase 2 categorizer's job (nothing writes it yet, so the endpoint would
return one anonymous row today).
"""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DbSession
from app.schemas.insights import (
    CategoryBreakdownQuery,
    CategoryBreakdownResponse,
    CategorySpend,
    PeriodTotals,
    SummaryQuery,
    SummaryResponse,
)
from app.services import insights as insights_service

router = APIRouter(prefix="/insights", tags=["insights"])


@router.get(
    "/summary",
    response_model=SummaryResponse,
    summary="Income, spending and net for one month",
)
def monthly_summary(
    user: CurrentUser, db: DbSession, query: Annotated[SummaryQuery, Query()]
) -> SummaryResponse:
    """What one month did to your balance, next to the month before it.

    Self transfers are excluded from every total: money you moved between
    your own accounts is neither income nor spending.

    The two `*_change_pct` fields are `null` when the previous month had
    nothing to compare against - growth from zero has no percentage.
    """
    month = query.resolved_month()
    summary = insights_service.monthly_summary(db, user.id, month)
    return SummaryResponse(
        month=month.label,
        date_from=month.first_day,
        date_to=month.last_day,
        current=_totals(summary.current),
        previous=_totals(summary.previous),
        income_change_pct=summary.income_change_pct,
        spend_change_pct=summary.spend_change_pct,
    )


@router.get(
    "/categories",
    response_model=CategoryBreakdownResponse,
    summary="Spending per category over a date range",
)
def category_breakdown(
    user: CurrentUser, db: DbSession, query: Annotated[CategoryBreakdownQuery, Query()]
) -> CategoryBreakdownResponse:
    """Where your money went between two dates, biggest category first.

    Spending only (debits, self transfers excluded). Transactions you have
    not categorized yet are grouped into one row with `category_id: null`,
    so the parts always add up to `total_spend_paise`.

    `from` and `to` are both inclusive, and are either both given or both
    left out - with neither, the range is the current month.
    """
    date_from, date_to = query.resolved_range()
    breakdown = insights_service.category_breakdown(db, user.id, date_from, date_to)
    return CategoryBreakdownResponse(
        date_from=breakdown.date_from,
        date_to=breakdown.date_to,
        total_spend_paise=breakdown.total_spend_paise,
        items=[
            CategorySpend.model_validate(item, from_attributes=True) for item in breakdown.items
        ],
    )


def _totals(totals: insights_service.PeriodTotals) -> PeriodTotals:
    """Service dataclass -> response model.

    Written out rather than `model_validate(..., from_attributes=True)`
    because `month` changes shape on the way out: the service carries a
    `Month` value object, the JSON carries its "2026-08" label. `net_paise`
    is a property on the dataclass and a stored field on the response, so
    it is copied here too.
    """
    return PeriodTotals(
        month=totals.month.label,
        income_paise=totals.income_paise,
        spend_paise=totals.spend_paise,
        net_paise=totals.net_paise,
        transaction_count=totals.transaction_count,
    )
