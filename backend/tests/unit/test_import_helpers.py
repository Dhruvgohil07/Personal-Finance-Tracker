"""Unit tests for the small decisions inside the import service.

These two helpers are private (`_safe_filename`, `_statement_period`), and
testing a private function is usually a smell. Here it is the right call:
both are pure functions whose edge cases need no database, no account and
no HTTP request, and reaching some of those cases through the API is
impossible - an HTTP client percent-encodes a control character in a
filename before it ever reaches the server, so only a direct call can prove
we would survive one.

The rest of the service is covered end to end in
tests/integration/test_uploads.py.
"""

import datetime

import pytest

from app.models.statement_upload import MAX_FILENAME_LENGTH
from app.parsers.base import Direction, ParsedStatement, RawRow
from app.services.imports import _safe_filename, _statement_period


def row(day: int) -> RawRow:
    return RawRow(
        row_number=day,
        txn_date=datetime.date(2026, 8, day),
        amount_paise=7000,
        direction=Direction.DEBIT,
        raw_description="SHOP",
    )


# --- _safe_filename -------------------------------------------------------


@pytest.mark.parametrize(
    ("sent", "stored"),
    [
        ("statement.xls", "statement.xls"),
        # A full Windows path: only the last part is a name.
        ("C:\\Users\\me\\Downloads\\statement.xls", "statement.xls"),
        ("/home/me/statement.csv", "statement.csv"),
        # Control characters have no place in a label, and a NUL cannot be
        # stored in a Postgres text column at all - the driver would raise
        # on INSERT and the user would see a 500.
        ("state\x00ment.xls", "statement.xls"),
        ("state\nment.xls", "statement.xls"),
        ("statement.xls\x7f", "statement.xls"),
        ("  statement.xls  ", "statement.xls"),
        # Nothing usable left: a label is still needed for the upload list.
        ("", "statement"),
        ("\\\\", "statement"),
        ("   ", "statement"),
    ],
)
def test_filenames_are_cleaned(sent: str, stored: str) -> None:
    assert _safe_filename(sent) == stored


def test_an_overlong_filename_is_truncated() -> None:
    """The column is VARCHAR(255): cut it here, rather than let the database
    reject the whole import over a label."""
    cleaned = _safe_filename("x" * 1000 + ".xls")

    assert len(cleaned) == MAX_FILENAME_LENGTH


# --- _statement_period ----------------------------------------------------


def test_the_statements_own_period_wins() -> None:
    """ICICI prints the period in its header; it is more truthful than the
    rows, because a month with no transactions still has a period."""
    statement = ParsedStatement(
        rows=[row(2), row(3)],
        period_start=datetime.date(2026, 8, 1),
        period_end=datetime.date(2026, 8, 31),
    )

    assert _statement_period(statement) == (datetime.date(2026, 8, 1), datetime.date(2026, 8, 31))


def test_the_row_dates_are_the_fallback() -> None:
    """A bare CSV states no period, so the first and last transaction date
    are the best answer available."""
    statement = ParsedStatement(rows=[row(3), row(1), row(2)])

    assert _statement_period(statement) == (datetime.date(2026, 8, 1), datetime.date(2026, 8, 3))


def test_half_a_period_also_falls_back_to_the_rows() -> None:
    """A period with only one end is not usable as a range."""
    statement = ParsedStatement(rows=[row(2)], period_start=datetime.date(2026, 8, 1))

    assert _statement_period(statement) == (datetime.date(2026, 8, 2), datetime.date(2026, 8, 2))


def test_a_statement_with_no_rows_and_no_period_has_none() -> None:
    """Which is why both columns are nullable: a dormant month is quiet,
    not corrupt (ADR 004)."""
    assert _statement_period(ParsedStatement()) == (None, None)
