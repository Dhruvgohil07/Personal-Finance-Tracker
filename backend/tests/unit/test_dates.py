"""Unit tests for app/utils/dates.py: the IST clock and the `Month` value object.

No database and no HTTP here - these are the calendar rules the insights
endpoints rest on, and getting one of them wrong would quietly report the
wrong month's spending rather than fail. The cases that matter:

- `test_the_month_before_january_is_december_of_the_previous_year`
- `test_the_last_day_of_february_knows_about_leap_years`
- `test_today_in_ist_is_ahead_of_utc_by_five_and_a_half_hours`
"""

import datetime
from datetime import timedelta

import pytest

from app.utils import dates
from app.utils.dates import IST, MAX_YEAR, MIN_YEAR, Month, today_in_ist

# --- The IST clock ----------------------------------------------------------


def test_ist_is_utc_plus_five_thirty() -> None:
    assert IST.utcoffset(None) == timedelta(hours=5, minutes=30)


def test_today_in_ist_is_the_date_in_india_not_in_utc() -> None:
    """The reason this module exists (CLAUDE.md rule 10).

    Rather than freezing the clock, this compares the two answers: the IST
    date is either the UTC date or one day ahead, never behind, and the
    gap is only ever the 18:30-00:00 UTC window.
    """
    utc_now = datetime.datetime.now(datetime.UTC)
    difference = today_in_ist() - utc_now.date()

    assert difference in (timedelta(0), timedelta(days=1))
    # After 18:30 UTC, India is already on the next day.
    expected_ahead = utc_now.hour > 18 or (utc_now.hour == 18 and utc_now.minute >= 30)
    assert (difference == timedelta(days=1)) is expected_ahead


def test_today_in_ist_is_read_per_call_not_at_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """A module-level constant would freeze the day the process started.

    Proved by moving the clock: `today_in_ist` must follow it.
    """

    class FrozenDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz: datetime.tzinfo | None = None) -> datetime.datetime:
            return datetime.datetime(2026, 8, 15, 10, 0, tzinfo=tz)

    monkeypatch.setattr(dates, "datetime", FrozenDatetime)

    assert today_in_ist() == datetime.date(2026, 8, 15)


# --- Building a Month -------------------------------------------------------


def test_parse_reads_a_yyyy_mm_string() -> None:
    assert Month.parse("2026-08") == Month(year=2026, month=8)


@pytest.mark.parametrize(
    "text",
    [
        "2026-13",  # month out of range
        "2026-00",  # month zero
        "2026-8",  # month not zero padded
        "26-08",  # two-digit year
        "2026/08",  # wrong separator
        "August 2026",
        "2026-08-01",  # a date, not a month
        "",
        " 2026-08 ",  # not stripped: a query parameter is stripped upstream
    ],
)
def test_parse_rejects_anything_else(text: str) -> None:
    with pytest.raises(ValueError, match="YYYY-MM"):
        Month.parse(text)


@pytest.mark.parametrize("year", [MIN_YEAR - 1, MAX_YEAR + 1, 0])
def test_a_year_outside_the_supported_range_is_refused(year: int) -> None:
    with pytest.raises(ValueError, match="year must be between"):
        Month(year=year, month=1)


@pytest.mark.parametrize("month", [0, 13, -1])
def test_a_month_outside_one_to_twelve_is_refused(month: int) -> None:
    with pytest.raises(ValueError, match="month must be between"):
        Month(year=2026, month=month)


def test_a_month_cannot_be_changed_after_it_is_built() -> None:
    """Frozen: a value object that could be edited in place is not a value."""
    august = Month(year=2026, month=8)

    with pytest.raises(AttributeError):
        august.month = 9  # type: ignore[misc]


def test_from_date_takes_the_month_a_day_falls_in() -> None:
    assert Month.from_date(datetime.date(2026, 8, 31)) == Month(year=2026, month=8)


def test_current_is_the_month_it_is_in_india() -> None:
    today = today_in_ist()

    assert Month.current() == Month(year=today.year, month=today.month)


# --- Reading a Month --------------------------------------------------------


def test_label_is_zero_padded() -> None:
    assert Month(year=2026, month=8).label == "2026-08"
    assert Month(year=2026, month=12).label == "2026-12"


def test_label_round_trips_through_parse() -> None:
    month = Month(year=2026, month=3)

    assert Month.parse(month.label) == month


def test_first_day_is_the_first() -> None:
    assert Month(year=2026, month=8).first_day == datetime.date(2026, 8, 1)


@pytest.mark.parametrize(
    ("year", "month", "expected_day"),
    [
        (2026, 1, 31),
        (2026, 4, 30),
        (2026, 2, 28),  # not a leap year
        (2024, 2, 29),  # a leap year
        (2000, 2, 29),  # divisible by 400: a leap year
        (1900, 2, 28),  # divisible by 100 but not 400: not a leap year
        (2026, 12, 31),
    ],
)
def test_last_day_of_the_month(year: int, month: int, expected_day: int) -> None:
    assert Month(year=year, month=month).last_day == datetime.date(year, month, expected_day)


# --- The previous month -----------------------------------------------------


def test_previous_month_inside_a_year() -> None:
    assert Month(year=2026, month=8).previous() == Month(year=2026, month=7)


def test_the_month_before_january_is_december_of_the_previous_year() -> None:
    """The off-by-one the summary endpoint would otherwise get wrong."""
    assert Month(year=2026, month=1).previous() == Month(year=2025, month=12)


def test_the_first_supported_month_has_no_previous_month() -> None:
    """Why `SummaryQuery` validates `previous()` instead of the service.

    Reaching this through the API would be a 500; the schema turns it into
    a 422 naming the field (see app/schemas/insights.py).
    """
    with pytest.raises(ValueError, match="year must be between"):
        Month(year=MIN_YEAR, month=1).previous()


def test_months_sort_chronologically() -> None:
    """`order=True` compares year before month, so December is not "biggest"."""
    months = [Month(year=2026, month=1), Month(year=2025, month=12), Month(year=2026, month=2)]

    assert sorted(months) == [
        Month(year=2025, month=12),
        Month(year=2026, month=1),
        Month(year=2026, month=2),
    ]


def test_a_month_can_be_a_dictionary_key() -> None:
    """Frozen + hashable is what lets the service index SQL rows by month."""
    totals = {Month(year=2026, month=8): "august"}

    assert totals[Month.parse("2026-08")] == "august"
