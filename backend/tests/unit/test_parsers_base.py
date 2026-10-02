"""Tests for app.parsers.base: the shared types and the layering rule."""

import ast
from datetime import date
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


def _imported_modules(source: str) -> set[str]:
    """Every module name imported by a Python file, via `ast`.

    Reading the source instead of importing it and inspecting `sys.modules`
    keeps the test honest: an import that only happens inside a function
    still counts, and nothing has to be importable for the test to run.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
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
