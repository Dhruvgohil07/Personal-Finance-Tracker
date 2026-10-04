"""Tests for app.parsers.base: the shared types and the layering rule."""

import ast
from datetime import date, datetime
from pathlib import Path

import pytest

import app.parsers
from app.parsers.base import (
    Direction,
    FileType,
    NormalizedRow,
    ParsedStatement,
    ParseError,
    ParserInput,
    RawRow,
)

# --- ParseError -----------------------------------------------------------


def test_parse_error_includes_the_row_number() -> None:
    error = ParseError("date column is empty", row_number=12)

    assert str(error) == "row 12: date column is empty"
    assert error.reason == "date column is empty"
    assert error.row_number == 12


def test_parse_error_without_a_row_number_is_just_the_reason() -> None:
    error = ParseError("file has no header row")

    assert str(error) == "file has no header row"
    assert error.row_number is None


# --- RawRow ---------------------------------------------------------------


def _raw_row(**overrides: object) -> RawRow:
    """A valid RawRow, with single fields replaced per test."""
    fields: dict[str, object] = {
        "row_number": 1,
        "txn_date": date(2026, 8, 14),
        "amount_paise": 125050,
        "direction": Direction.DEBIT,
        "raw_description": "UPI/428913456789/ZOMATO",
    }
    return RawRow(**(fields | overrides))  # type: ignore[arg-type]


def test_raw_row_keeps_what_it_was_given() -> None:
    row = _raw_row(balance_after_paise=5000000, value_date=date(2026, 8, 15))

    assert row.amount_paise == 125050
    assert row.direction is Direction.DEBIT
    assert row.balance_after_paise == 5000000
    assert row.value_date == date(2026, 8, 15)


def test_optional_columns_default_to_none() -> None:
    row = _raw_row()

    assert row.value_date is None
    assert row.balance_after_paise is None


@pytest.mark.parametrize("amount", [0, -1, -125050])
def test_amount_must_be_positive(amount: int) -> None:
    # The sign lives in `direction`; a zero or negative amount means the
    # parser read the wrong column (ADR 001).
    with pytest.raises(ParseError) as exc_info:
        _raw_row(row_number=7, amount_paise=amount)

    assert exc_info.value.row_number == 7
    assert "greater than zero" in exc_info.value.reason


@pytest.mark.parametrize("amount", [1250.5, "1250", None, True])
def test_amount_must_be_an_integer(amount: object) -> None:
    # True is in the list on purpose: bool is a subclass of int, so a plain
    # `> 0` check would accept it as 1 paisa.
    with pytest.raises(ParseError, match="integer paise"):
        _raw_row(amount_paise=amount)


@pytest.mark.parametrize("balance", [2500000.0, "2500000", True])
def test_balance_must_be_an_integer_when_present(balance: object) -> None:
    # The balance is money too (ADR 001) and it goes into the fingerprint,
    # so a float would not fail - it would just hash as "2500000.0" and
    # stop matching the same row read from another file format.
    # Numeric .xls cells arrive from xlrd as floats, so this is the field
    # most likely to get one.
    with pytest.raises(ParseError, match="balance must be integer paise"):
        _raw_row(row_number=4, balance_after_paise=balance)


def test_a_missing_balance_is_allowed() -> None:
    # Plenty of statements have no balance column at all.
    assert _raw_row(balance_after_paise=None).balance_after_paise is None


@pytest.mark.parametrize("direction", ["debit", "DEBIT", None, 1])
def test_direction_must_be_a_direction_value(direction: object) -> None:
    # A plain "debit" string is the dangerous case: Direction is a StrEnum,
    # so `Direction.DEBIT == "debit"` and the string passes every
    # comparison in this package. It only breaks later, where the
    # fingerprint reads `.value` - a crash in the middle of an import.
    with pytest.raises(ParseError, match="direction must be"):
        _raw_row(row_number=9, direction=direction)


@pytest.mark.parametrize("field", ["txn_date", "value_date"])
def test_a_datetime_is_not_accepted_as_a_date(field: str) -> None:
    # datetime is a SUBCLASS of date, so isinstance(x, date) is True for it
    # and nothing would complain - but it formats as "2026-08-14T00:00:00"
    # instead of "2026-08-14", which changes the fingerprint. Statement
    # dates are whole days (SPEC §5 stores DATE), and
    # xlrd.xldate_as_datetime returns a datetime, so a parser that forgets
    # .date() has to be caught here.
    with pytest.raises(ParseError, match="must be a date"):
        _raw_row(**{field: datetime(2026, 8, 14, 10, 30)})


def test_a_plain_date_is_accepted() -> None:
    row = _raw_row(txn_date=date(2026, 8, 14), value_date=date(2026, 8, 15))

    assert row.txn_date.isoformat() == "2026-08-14"


def test_raw_row_is_frozen() -> None:
    row = _raw_row()

    # Immutable by design: once a row is parsed, later steps derive new
    # objects from it instead of editing it.
    with pytest.raises(AttributeError):
        row.amount_paise = 1  # type: ignore[misc]


# --- ParserInput / NormalizedRow -----------------------------------------


def test_parser_input_defaults_to_no_rows() -> None:
    data = ParserInput(file_type=FileType.CSV)

    assert data.rows == []
    assert data.file_type == "csv"  # StrEnum compares equal to its value


def test_normalized_row_wraps_the_raw_row() -> None:
    raw = _raw_row()
    row = NormalizedRow(raw=raw, normalized_description="UPI/ /ZOMATO", merchant_key="ZOMATO")

    # The amount is readable through the wrapper, and stored only once.
    assert row.raw.amount_paise == 125050
    assert row.merchant_key == "ZOMATO"


# --- ParsedStatement -----------------------------------------------------


def test_empty_statement_has_no_date_range() -> None:
    # A statement with no transactions is unusual but not corrupt (e.g. a
    # dormant account), so it parses and imports zero rows.
    assert ParsedStatement().row_date_range is None


def test_row_date_range_is_the_min_and_max_txn_date() -> None:
    statement = ParsedStatement(
        rows=[
            _raw_row(row_number=1, txn_date=date(2026, 8, 14)),
            _raw_row(row_number=2, txn_date=date(2026, 8, 2)),
            _raw_row(row_number=3, txn_date=date(2026, 8, 31)),
        ]
    )

    # Taken from the rows themselves, not from their order in the file:
    # some statements list newest first.
    assert statement.row_date_range == (date(2026, 8, 2), date(2026, 8, 31))


def test_statement_period_must_not_end_before_it_starts() -> None:
    with pytest.raises(ParseError, match="period"):
        ParsedStatement(period_start=date(2026, 9, 1), period_end=date(2026, 8, 1))


def test_a_one_day_statement_period_is_valid() -> None:
    statement = ParsedStatement(period_start=date(2026, 8, 1), period_end=date(2026, 8, 1))

    assert statement.period_start == statement.period_end


# --- The layering rule (SPEC §3) -----------------------------------------

# app/parsers must stay pure: no web framework, no database, no network, no
# app layers that pull those in. That is what lets every test in this file
# run with no Postgres, no Redis and no FastAPI app.
FORBIDDEN_IMPORTS = frozenset(
    {
        "fastapi",
        "starlette",
        "sqlalchemy",
        "alembic",
        "psycopg",
        "redis",
        "slowapi",
        "structlog",
        "httpx",
        "requests",
        "groq",
    }
)

# Inside the app, parsers may only use other pure modules.
ALLOWED_APP_MODULES = frozenset({"app.parsers", "app.utils"})


def _imported_modules(source: str, package: str = "app.parsers") -> set[str]:
    """Every module name imported by a Python file, via `ast`.

    Reading the source instead of importing it and inspecting `sys.modules`
    keeps the test honest: an import that only happens inside a function
    still counts, and nothing has to be importable for the test to run.

    Relative imports are resolved against `package`, because a guard that
    skipped them would be no guard at all: `from ..models.account import
    BankCode` must be reported as `app.models.account`. `node.level` is the
    number of leading dots - 1 means this package (`app.parsers`), 2 means
    its parent (`app`) - so dropping `level - 1` trailing parts of the
    package name and appending the module gives the absolute name.
    """
    names: set[str] = set()
    package_parts = package.split(".")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    names.add(node.module)
                continue
            base = package_parts[: len(package_parts) - (node.level - 1)]
            if node.module:
                names.add(".".join([*base, node.module]))
            else:
                # `from . import base` has no module name, and here the
                # imported *names* are themselves modules - unlike
                # `from .base import RawRow`, where RawRow is a class.
                names.update(".".join([*base, alias.name]) for alias in node.names)
    return names


# Found through the package itself (`__file__`) rather than a relative path,
# so the test does not care which directory pytest was started from.
PARSER_FILES = sorted(Path(app.parsers.__file__).parent.glob("*.py"))


@pytest.mark.parametrize("path", PARSER_FILES, ids=lambda path: path.name)
def test_parsers_package_imports_nothing_heavy(path: Path) -> None:
    for module in _imported_modules(path.read_text(encoding="utf-8")):
        top_level = module.split(".")[0]
        assert top_level not in FORBIDDEN_IMPORTS, f"{path.name} imports {module}"

        if top_level == "app":
            package = ".".join(module.split(".")[:2])
            assert package in ALLOWED_APP_MODULES, f"{path.name} imports {module}"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import sqlalchemy", {"sqlalchemy"}),
        ("import re, hashlib", {"re", "hashlib"}),
        ("from app.parsers.base import RawRow", {"app.parsers.base"}),
        # Relative imports must be resolved, not skipped: one dot is this
        # package, two dots is its parent.
        ("from .base import RawRow", {"app.parsers.base"}),
        ("from ..models.account import BankCode", {"app.models.account"}),
        ("from . import base", {"app.parsers.base"}),
        ("from .. import models", {"app.models"}),
        # An import hidden inside a function still counts.
        ("def f():\n    import sqlalchemy", {"sqlalchemy"}),
    ],
)
def test_import_scanner_sees_every_import_form(source: str, expected: set[str]) -> None:
    # Tests the test above. Without this, a scanner that quietly returned
    # nothing would make the layering guard pass for any file at all - which
    # is exactly what the `node.level == 0` filter used to do to relative
    # imports.
    assert _imported_modules(source) == expected


def test_the_layering_guard_would_catch_a_relative_sqlalchemy_import() -> None:
    # The end-to-end version of the check above, written the way the real
    # mistake would look: a parser reaching for the Account model.
    modules = _imported_modules("from ..models.account import BankCode")

    assert any(module.split(".")[0] == "app" for module in modules)
    assert not any(".".join(module.split(".")[:2]) in ALLOWED_APP_MODULES for module in modules)


# --- ParsedStatement.account_last4 ----------------------------------------


def test_account_last4_is_accepted_when_it_is_four_digits() -> None:
    statement = ParsedStatement(rows=[], account_last4="5606")

    assert statement.account_last4 == "5606"


@pytest.mark.parametrize("value", ["084601505606", "606", "56o6", ""])
def test_account_last4_rejects_anything_but_four_digits(value: str) -> None:
    """A guard against a parser passing on more than SPEC §7.3 allows."""
    with pytest.raises(ParseError, match="exactly 4 digits"):
        ParsedStatement(rows=[], account_last4=value)
