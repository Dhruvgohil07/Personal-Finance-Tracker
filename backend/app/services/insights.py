"""Insights: what came in, what went out, and where it went (SPEC §9.1).

Two questions, one table:

- `monthly_summary`     income, spend and net for a month, next to the
                        month before it and the % change between them
- `category_breakdown`  spending per category over any date range

**Why this module writes SQL by hand.** Everywhere else the ORM builds the
query, because everywhere else we are reading or writing *rows* and the ORM
is the thing that turns a row into a `Transaction` object. Here we never
want a row: we want `SUM(...) FILTER (WHERE ...) GROUP BY ...`, and the
answer is a handful of numbers. Written through the ORM that becomes a
stack of `func.sum(case(...))` calls that nobody can read aloud; written as
SQL it is the query you would type into psql, which is also the query you
can paste into `EXPLAIN ANALYZE` when it gets slow. SPEC §9.1 asks for
exactly that ("write these as clear SQL ... verify with EXPLAIN ANALYZE"),
and the measurements are in docs/decisions/010-insights-aggregation.md.

**Hand-written SQL does not mean string-built SQL.** Every value - the user
id, the dates, even the two direction constants - travels as a *bound
parameter* (`:user_id`), never as text pasted into the query
(CLAUDE.md §7.3). The SQL string below is a constant: it is identical for
every user and every request, which is what makes it safe and what lets
Postgres reuse its plan.

**Rule 2 in a module with no ORM.** `WHERE user_id = :user_id` is the first
condition of both queries and there is no code path without it. The
`categories` join in the second query is the one place worth a second look,
and it is safe: it only resolves the NAME of a category id already stored
on the user's own transaction, and a transaction can only carry a category
the user may use (`categories_service.get_category` enforces that on every
write).
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import CategoryKind
from app.parsers.base import Direction
from app.utils.dates import Month

# Shown for transactions the user has not filed and the categorizer has
# not reached (`category_id IS NULL`). See `CategorySpend` in
# app/schemas/insights.py for why this bucket is kept rather than dropped.
UNCATEGORIZED_NAME = "Uncategorized"

# Percentages are reported to one decimal place: "+12.5%" is as much
# precision as a spending figure can honestly carry.
_ONE_DECIMAL = Decimal("0.1")


# --- What the service returns ---------------------------------------------
# Plain frozen dataclasses, not Pydantic models: a service returns data and
# the route turns it into the response model (rule 11). They also keep the
# SQL rows from leaking into the API layer as anonymous tuples.


@dataclass(frozen=True)
class PeriodTotals:
    """Income, spending and the count behind them, for one month."""

    month: Month
    income_paise: int
    spend_paise: int
    transaction_count: int

    @property
    def net_paise(self) -> int:
        """What the month actually did to the balance; negative if overspent."""
        return self.income_paise - self.spend_paise

    @classmethod
    def empty(cls, month: Month) -> "PeriodTotals":
        """A month with no transactions: zeros, not a missing object.

        The SQL returns no row at all for such a month, and every caller
        would otherwise have to handle `None` - including the percentage
        maths, which already has its own zero case to think about.
        """
        return cls(month=month, income_paise=0, spend_paise=0, transaction_count=0)


@dataclass(frozen=True)
class MonthlySummary:
    current: PeriodTotals
    previous: PeriodTotals
    # None when the previous month had nothing to compare against; see
    # `percent_change`.
    income_change_pct: Decimal | None
    spend_change_pct: Decimal | None


@dataclass(frozen=True)
class CategorySpend:
    """One row of the breakdown. `category_id is None` = the uncategorized bucket."""

    category_id: uuid.UUID | None
    name: str
    slug: str | None
    kind: CategoryKind | None
    icon: str | None
    color: str | None
    amount_paise: int
    transaction_count: int
    share_pct: Decimal


@dataclass(frozen=True)
class CategoryBreakdown:
    date_from: date
    date_to: date
    total_spend_paise: int
    items: list[CategorySpend]


# --- The SQL --------------------------------------------------------------

# Both queries use `SUM(...) FILTER (WHERE ...)`, which is worth knowing if
# you have not met it: it is standard SQL for "aggregate only the rows
# matching this condition", so one pass over the month produces both the
# credit total and the debit total. The older way to write it is
# `SUM(CASE WHEN direction = 'credit' THEN amount_paise ELSE 0 END)`, which
# does the same work but hides the condition inside an expression.
#
# `COALESCE(..., 0)`: SUM over zero matching rows is NULL, not 0. A month
# with only debits would otherwise report income NULL, and NULL - anything
# is NULL, so `net` would vanish too.
#
# One surprise to know about: `SUM()` over a BIGINT column returns NUMERIC,
# not BIGINT, because a total can be larger than any single row and
# Postgres refuses to overflow silently. The driver therefore hands us a
# `Decimal`, and `int()` converts it at the boundary below - Python's int
# has no width limit, so nothing can overflow there either. Casting back to
# `::bigint` inside the query would be shorter and would throw that safety
# away. This is not pedantry: `format_inr` rejects anything that is not an
# `int` (ADR 001), so a Decimal slipping through would be a 500.
#
# `date_trunc('month', txn_date)::date` turns every date in a month into
# that month's 1st, which is what GROUP BY needs to collapse the window
# into one row per month. The cast back to `date` is so psycopg hands us a
# `datetime.date` rather than a midnight timestamp.
_MONTHLY_TOTALS_SQL = text("""
    SELECT
        date_trunc('month', txn_date)::date AS month_start,
        COALESCE(SUM(amount_paise) FILTER (WHERE direction = :credit), 0) AS income_paise,
        COALESCE(SUM(amount_paise) FILTER (WHERE direction = :debit), 0)  AS spend_paise,
        COUNT(*) AS transaction_count
    FROM transactions
    WHERE user_id = :user_id
      AND txn_date >= :start_date
      AND txn_date <= :end_date
      AND NOT is_self_transfer
    GROUP BY month_start
""")

# LEFT JOIN, not JOIN: a transaction with no category still has to appear,
# as the uncategorized bucket. An inner join would silently drop those rows
# and the parts would no longer add up to the total - which in Phase 1,
# where almost nothing is categorized yet, would make the endpoint look
# empty rather than honest.
#
# Every selected category column is in the GROUP BY. They are all
# functionally dependent on `t.category_id`, but Postgres only infers that
# from a grouped PRIMARY KEY of the table the columns come from, and we
# group by the foreign key on the other side.
_CATEGORY_SPEND_SQL = text("""
    SELECT
        t.category_id,
        c.name,
        c.slug,
        c.kind,
        c.icon,
        c.color,
        SUM(t.amount_paise) AS amount_paise,
        COUNT(*) AS transaction_count
    FROM transactions AS t
    LEFT JOIN categories AS c ON c.id = t.category_id
    WHERE t.user_id = :user_id
      AND t.txn_date >= :start_date
      AND t.txn_date <= :end_date
      AND t.direction = :debit
      AND NOT t.is_self_transfer
    GROUP BY t.category_id, c.name, c.slug, c.kind, c.icon, c.color
    ORDER BY amount_paise DESC, c.name ASC
""")


# --- The service ----------------------------------------------------------


def monthly_summary(db: Session, user_id: uuid.UUID, month: Month) -> MonthlySummary:
    """Totals for `month`, with the month before it and the % change.

    One query covers both months. The alternative - running the same query
    twice with different dates - costs two round trips and two index scans
    for data that sits next to each other in the same index, so the window
    is widened to start on the 1st of the previous month and the two rows
    are sorted out in Python.

    A month with no transactions simply has no row in the result, and
    becomes `PeriodTotals.empty(...)`: zeros all round, which is the
    truthful answer for a month in which nothing happened.
    """
    previous_month = month.previous()
    rows = db.execute(
        _MONTHLY_TOTALS_SQL,
        {
            "user_id": user_id,
            # The window spans both months, so one scan answers both.
            "start_date": previous_month.first_day,
            "end_date": month.last_day,
            "credit": Direction.CREDIT.value,
            "debit": Direction.DEBIT.value,
        },
    ).all()

    # Keyed by the month each row belongs to, so neither month depends on
    # the order the database happened to return them in.
    totals = {
        Month.from_date(row.month_start): PeriodTotals(
            month=Month.from_date(row.month_start),
            # int(), because SUM() of a BIGINT column arrives as a Decimal
            # (see the note above the SQL).
            income_paise=int(row.income_paise),
            spend_paise=int(row.spend_paise),
            transaction_count=row.transaction_count,
        )
        for row in rows
    }
    current = totals.get(month) or PeriodTotals.empty(month)
    previous = totals.get(previous_month) or PeriodTotals.empty(previous_month)

    return MonthlySummary(
        current=current,
        previous=previous,
        income_change_pct=percent_change(current.income_paise, previous.income_paise),
        spend_change_pct=percent_change(current.spend_paise, previous.spend_paise),
    )


def category_breakdown(
    db: Session, user_id: uuid.UUID, date_from: date, date_to: date
) -> CategoryBreakdown:
    """Spending per category between two dates, biggest first.

    Debits only and self transfers excluded (SPEC §9.1), so this answers
    "where did my money go?" and nothing else. Income categories therefore
    never appear - a month's salary would otherwise dwarf every real
    spending row and make the chart useless.

    The share each category takes is worked out here rather than in SQL
    (`SUM(...) OVER ()` would do it): the totals are already in memory, the
    arithmetic is exact integer division into a Decimal, and keeping it in
    Python means the percentages and the total can never disagree with the
    rows they were derived from.
    """
    rows = db.execute(
        _CATEGORY_SPEND_SQL,
        {
            "user_id": user_id,
            "start_date": date_from,
            "end_date": date_to,
            "debit": Direction.DEBIT.value,
        },
    ).all()

    # int() for the same reason as in `monthly_summary`: SUM() of a BIGINT
    # column comes back as a Decimal. The total is needed before the shares
    # can be worked out, hence the two passes.
    amounts = [int(row.amount_paise) for row in rows]
    total_spend_paise = sum(amounts)
    items = [
        CategorySpend(
            category_id=row.category_id,
            # A LEFT JOIN that found nothing leaves every `c.` column NULL,
            # which is exactly how the uncategorized bucket arrives.
            name=row.name if row.category_id is not None else UNCATEGORIZED_NAME,
            slug=row.slug,
            kind=CategoryKind(row.kind) if row.kind is not None else None,
            icon=row.icon,
            color=row.color,
            amount_paise=amount,
            transaction_count=row.transaction_count,
            share_pct=_share_pct(amount, total_spend_paise),
        )
        # strict=True: if the two lists ever drifted apart, every amount
        # after the gap would be attached to the wrong category. Python
        # raises instead of zipping to the shorter one.
        for row, amount in zip(rows, amounts, strict=True)
    ]
    return CategoryBreakdown(
        date_from=date_from,
        date_to=date_to,
        total_spend_paise=total_spend_paise,
        items=items,
    )


# --- Pure arithmetic ------------------------------------------------------


def percent_change(current: int, previous: int) -> Decimal | None:
    """How much `current` grew over `previous`, in %, or None.

        percent_change(1500, 1000) -> Decimal("50.0")
        percent_change(500, 1000)  -> Decimal("-50.0")
        percent_change(1000, 0)    -> None

    **None, not 0 and not 100, when the previous month was zero.** There is
    no percentage that describes growth from nothing: every value is
    infinitely larger than zero. Returning 0 would draw a flat line where
    there is no line, and returning 100 would invent a comparison. `null`
    reaches the client, which can then write "no data for August" instead
    of a meaningless number.

    Decimal, not float, for the same reason money is: the division is
    exact in base 10 and the rounding at the end is the rounding a reader
    expects (0.05 up, not "to the nearest even"). The result is a
    percentage and never money, so rule 1 is not at stake here - this is
    about the number being reproducible.
    """
    if previous == 0:
        return None
    change = Decimal(current - previous) / Decimal(previous) * 100
    return change.quantize(_ONE_DECIMAL, rounding=ROUND_HALF_UP)


def _share_pct(amount: int, total: int) -> Decimal:
    """What fraction of `total` this `amount` is, in %.

    The zero guard is unreachable through `category_breakdown` (a total of
    zero means there were no rows to compute a share for, so the loop does
    not run), but a divide-by-zero in a reporting endpoint is not something
    to leave to an argument about reachability.
    """
    if total == 0:
        return Decimal("0.0")
    return (Decimal(amount) / Decimal(total) * 100).quantize(_ONE_DECIMAL, rounding=ROUND_HALF_UP)
