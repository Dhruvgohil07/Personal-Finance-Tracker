"""Unit tests for the pure arithmetic and validation behind /insights.

Three things are tested without a database, because they are decisions
rather than queries:

- `percent_change` - and in particular that growth from zero is `None`,
  not 0 and not 100 (see the function's docstring for why)
- `_share_pct`     - a category's share of total spending
- the two query models - which month/range a request actually means, and
  which requests are refused

The SQL itself is covered end to end in tests/integration/test_insights.py.
"""

import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.insights import CategoryBreakdownQuery, SummaryQuery
from app.services.insights import _share_pct, percent_change
from app.utils.dates import MIN_YEAR, Month

# --- percent_change ---------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "previous", "expected"),
    [
        (150_000, 100_000, "50.0"),  # spent half again as much
        (50_000, 100_000, "-50.0"),  # spent half as much
        (100_000, 100_000, "0.0"),  # exactly the same
        (200_000, 100_000, "100.0"),  # doubled
        (0, 100_000, "-100.0"),  # stopped entirely
        (300_000, 100_000, "200.0"),  # tripled
    ],
)
def test_percent_change_against_a_non_zero_month(
    current: int, previous: int, expected: str
) -> None:
    assert percent_change(current, previous) == Decimal(expected)


def test_growth_from_zero_has_no_percentage() -> None:
    """The case the plan called out: "safe when the previous month is 0".

    `None` reaches the client as JSON `null`, which it can render as "no
    comparison" instead of a number that would be a lie either way.
    """
    assert percent_change(500_000, 0) is None


def test_two_empty_months_also_have_no_percentage() -> None:
    """0 -> 0 is "no change", but there is still nothing to divide by."""
    assert percent_change(0, 0) is None


def test_percent_change_rounds_to_one_decimal_place_half_up() -> None:
    """3 of 7 is 42.857...%, and a reader expects 42.9, not 42.8.

    Python's own `round()` would give 42.8 here (it rounds halves to even),
    which is why the function quantizes with ROUND_HALF_UP explicitly.
    """
    assert percent_change(1000, 700) == Decimal("42.9")
    assert percent_change(1005, 1000) == Decimal("0.5")
    # 1.25% exactly: the half rounds up, not to the even digit.
    assert percent_change(10_125, 10_000) == Decimal("1.3")


def test_percent_change_is_a_decimal_never_a_float() -> None:
    """A float would make the same input produce different text over time."""
    result = percent_change(100_000, 300_000)

    assert isinstance(result, Decimal)
    # -66.666...% rounded to one place.
    assert result == Decimal("-66.7")


def test_percent_change_survives_very_large_amounts() -> None:
    """Paise are BIGINT, so the arithmetic must not lose precision."""
    assert percent_change(2_000_000_000_000, 1_000_000_000_000) == Decimal("100.0")


# --- _share_pct -------------------------------------------------------------


def test_shares_of_the_total() -> None:
    assert _share_pct(25_000, 100_000) == Decimal("25.0")
    assert _share_pct(100_000, 100_000) == Decimal("100.0")
    assert _share_pct(1, 100_000) == Decimal("0.0")  # rounds down to nothing


def test_shares_of_a_three_way_split_add_up_to_about_a_hundred() -> None:
    """Thirds cannot each be exact, so this documents what is returned.

    33.3 x 3 = 99.9, not 100: rounding to one decimal place cannot be made
    to add up, and the client is given `total_spend_paise` for the exact
    figure. Anything else would mean adjusting one row's percentage to
    cover the gap, which would make that row's number wrong.
    """
    shares = [_share_pct(100, 300) for _ in range(3)]

    assert shares == [Decimal("33.3")] * 3
    assert sum(shares) == Decimal("99.9")


def test_a_share_of_nothing_is_zero_not_a_crash() -> None:
    assert _share_pct(0, 0) == Decimal("0.0")


# --- SummaryQuery -----------------------------------------------------------


def test_no_month_means_the_current_month_in_india() -> None:
    assert SummaryQuery().resolved_month() == Month.current()


def test_a_blank_month_is_treated_as_not_sent() -> None:
    """Inherited from `QueryFilters`: `?month=` is "no month", not a 422."""
    assert SummaryQuery.model_validate({"month": ""}).resolved_month() == Month.current()


def test_a_given_month_is_the_month_used() -> None:
    assert SummaryQuery(month="2026-08").resolved_month() == Month(year=2026, month=8)


# Values that are not a month at all. A BLANK month is deliberately not in
# here: `QueryFilters` drops it before validation ("blank = not sent"), and
# `test_a_blank_month_is_treated_as_not_sent` above covers that.
MALFORMED_MONTHS = ("2026-13", "2026-8", "August", "2026-08-01", "0000-01", "2026-08\n")


@pytest.mark.parametrize("month", MALFORMED_MONTHS)
def test_a_malformed_month_is_rejected(month: str) -> None:
    with pytest.raises(ValidationError):
        SummaryQuery(month=month)


@pytest.mark.parametrize("month", MALFORMED_MONTHS)
def test_the_schema_and_month_parse_agree_on_what_is_malformed(month: str) -> None:
    """The two validators of `month` must never disagree.

    `MONTH_PATTERN` is applied twice over: by Pydantic (the Rust regex
    engine, as a search) and by `Month.parse` (Python's `re`, as a
    fullmatch). A value that one accepts and the other rejects is exactly
    how "2026-08\\n" got through `Month.parse` while the endpoint still
    answered 422 - the bug this test exists to stop coming back.

    Anything the schema refuses, `Month.parse` must refuse too.
    """
    with pytest.raises(ValidationError):
        SummaryQuery(month=month)
    with pytest.raises(ValueError):
        Month.parse(month)


def test_a_month_with_no_previous_month_is_rejected_by_the_schema() -> None:
    """So the service never raises: the summary always has a month to compare to."""
    with pytest.raises(ValidationError, match="year must be between"):
        SummaryQuery(month=f"{MIN_YEAR}-01")

    # One month later is fine - its previous month is still in range.
    assert SummaryQuery(month=f"{MIN_YEAR}-02").resolved_month() == Month(year=MIN_YEAR, month=2)


def test_an_unknown_parameter_is_rejected() -> None:
    """`?months=12` is the trend endpoint's parameter, not this one's."""
    with pytest.raises(ValidationError):
        SummaryQuery.model_validate({"months": "12"})


# --- CategoryBreakdownQuery -------------------------------------------------


def test_no_dates_mean_the_current_month() -> None:
    current = Month.current()

    assert CategoryBreakdownQuery().resolved_range() == (current.first_day, current.last_day)


def test_both_dates_given_are_used_as_they_are() -> None:
    query = CategoryBreakdownQuery.model_validate({"from": "2026-08-01", "to": "2026-08-31"})

    assert query.resolved_range() == (datetime.date(2026, 8, 1), datetime.date(2026, 8, 31))


def test_a_single_day_range_is_allowed() -> None:
    """from == to: "what did I spend on the 4th?"."""
    query = CategoryBreakdownQuery.model_validate({"from": "2026-08-04", "to": "2026-08-04"})

    assert query.resolved_range() == (datetime.date(2026, 8, 4), datetime.date(2026, 8, 4))


@pytest.mark.parametrize("params", [{"from": "2026-08-01"}, {"to": "2026-08-31"}])
def test_half_a_range_is_refused(params: dict[str, str]) -> None:
    """Both or neither - see the class docstring for why one is not defaulted."""
    with pytest.raises(ValidationError, match="both 'from' and 'to', or neither"):
        CategoryBreakdownQuery.model_validate(params)


def test_a_backwards_range_is_refused() -> None:
    with pytest.raises(ValidationError, match="must not be later than"):
        CategoryBreakdownQuery.model_validate({"from": "2026-09-01", "to": "2026-08-01"})


def test_both_dates_blank_is_treated_as_not_sent() -> None:
    current = Month.current()
    query = CategoryBreakdownQuery.model_validate({"from": "", "to": ""})

    assert query.resolved_range() == (current.first_day, current.last_day)


def test_the_python_field_names_are_not_query_parameters() -> None:
    """The query string spells them "from"/"to" (SPEC §10), like /transactions."""
    with pytest.raises(ValidationError):
        CategoryBreakdownQuery.model_validate({"date_from": "2026-08-01", "date_to": "2026-08-31"})
