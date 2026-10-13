"""Dates and calendar months, as the app understands them.

A pure module: no database, no HTTP, no FastAPI. Two jobs.

**1. "Today" means today in India.** CLAUDE.md rule 10 fixes the app
timezone at Asia/Kolkata. That is not decoration: `GET /insights/summary`
with no `month` has to pick a month, and a server running in UTC would
pick the wrong one for five and a half hours of every day - on the 1st of
a month at 02:00 IST, UTC is still on the 31st of the previous month, so
the user would be shown last month's spending as "this month".

**2. A calendar month is a value, not three loose integers.** The insights
endpoints need the first and last day of a month, and the month before it
(for the "% vs previous month" comparison). `Month` holds that in one
object so no caller ever has to remember that February has 28 days, or
that the month before January is in the previous year.

Why a fixed UTC+05:30 offset instead of `zoneinfo.ZoneInfo("Asia/Kolkata")`:
India has observed no daylight saving time since 1945 and IST is
permanently UTC+05:30, so there is nothing for a timezone database to
decide. A fixed offset is also one less moving part on Windows, where
`zoneinfo` has no system tz database to read and would need the extra
`tzdata` package installed. If India ever adopts DST, this is the one
place to change.
"""

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

# Asia/Kolkata: UTC+05:30, all year, every year (see the module docstring).
IST = timezone(timedelta(hours=5, minutes=30), name="IST")

# `month=YYYY-MM`, the format SPEC §10 gives for `GET /insights/summary`.
# The month part is restricted to 01-12 by the pattern itself, so a schema
# can use it as a Pydantic `pattern=` and reject "2026-13" before any
# Python runs.
#
# This one string is read by TWO different regex engines, which anchor
# differently, so it is belt AND braces on purpose:
#
# - Pydantic compiles `pattern=` with the RUST regex crate and applies it
#   as a SEARCH, so without the `^...$` anchors "xx2026-08xx" would pass.
#   (Rust spells end-of-string `\z` and Python spells it `\Z`, so neither
#   is portable between the two - `$` is the only anchor both understand.)
# - Python's `re` understands `$` as "end of string, OR just before a
#   trailing newline", so `_MONTH_RE.match("2026-08\n")` succeeds. That is
#   why `Month.parse` below uses `fullmatch`, which additionally requires
#   the whole string to be consumed.
#
# Drop either half and the two engines stop agreeing on what is valid.
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
_MONTH_RE = re.compile(MONTH_PATTERN)

# A bank statement is never from the year 7, and `Month.previous()` must
# always be representable (the month before 0001-01 does not exist), so
# the year is bounded. Generous on purpose: the limits exist to turn
# nonsense into a clear error, not to predict how long this app lives.
MIN_YEAR = 1900
MAX_YEAR = 2999


def today_in_ist() -> date:
    """The current date in India (CLAUDE.md rule 10).

    Used as the default when a request does not say which period it means.
    Written as a function, not a constant, so it is evaluated per request -
    a module-level constant would freeze the day the process started.
    """
    return datetime.now(IST).date()


@dataclass(frozen=True, order=True)
class Month:
    """One calendar month, e.g. August 2026.

    Frozen (immutable) and ordered, so months can be compared and used as
    dictionary keys - which is exactly what the summary service does when
    it sorts two months of SQL results into "current" and "previous".

    `order=True` compares the fields in the order they are declared, so
    year is compared before month: 2026-01 > 2025-12, as it should be.
    """

    year: int
    month: int

    def __post_init__(self) -> None:
        # A frozen dataclass still runs __post_init__, which is where a
        # value object checks that it is a valid one. Building an invalid
        # Month is impossible, so no caller has to re-check.
        if not MIN_YEAR <= self.year <= MAX_YEAR:
            raise ValueError(f"year must be between {MIN_YEAR} and {MAX_YEAR}")
        if not 1 <= self.month <= 12:
            raise ValueError("month must be between 1 and 12")

    @classmethod
    def from_date(cls, day: date) -> "Month":
        """The month a given day falls in."""
        return cls(year=day.year, month=day.month)

    @classmethod
    def current(cls) -> "Month":
        """The month it is in India right now."""
        return cls.from_date(today_in_ist())

    @classmethod
    def parse(cls, text: str) -> "Month":
        """Read a "YYYY-MM" string, or raise `ValueError`.

        `ValueError` and not a custom exception: raised inside a Pydantic
        validator, a ValueError becomes our standard 422 naming the field
        (see app/core/errors.py), which is exactly the behaviour wanted.

        `fullmatch`, not `match`: Python's `$` also matches just before a
        trailing newline, so `match` would accept "2026-08\\n" even though
        the pattern looks anchored. `fullmatch` additionally requires the
        whole string to be consumed. See the note above `MONTH_PATTERN`.
        """
        if _MONTH_RE.fullmatch(text) is None:
            raise ValueError("month must look like YYYY-MM, e.g. 2026-08")
        year, month = text.split("-")
        return cls(year=int(year), month=int(month))

    @property
    def label(self) -> str:
        """The month back in "YYYY-MM" form, for the API response."""
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def first_day(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def last_day(self) -> date:
        """The last day of this month, leap years included.

        `calendar.monthrange(2026, 2)` returns `(weekday_of_the_1st, 28)`,
        and `(..., 29)` for February 2024 - the standard library owns the
        leap-year rule so we do not have to.
        """
        return date(self.year, self.month, calendar.monthrange(self.year, self.month)[1])

    def previous(self) -> "Month":
        """The month before this one, crossing the year boundary correctly."""
        if self.month == 1:
            return Month(year=self.year - 1, month=12)
        return Month(year=self.year, month=self.month - 1)
