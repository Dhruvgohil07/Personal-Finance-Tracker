"""Tests for `ColumnMappingRequest` (app/schemas/upload.py).

This schema is the API edge of the parser's `ColumnMapping` dataclass. Its
job is to turn a bad mapping into a 422 naming the field, instead of
letting it reach the parser and fail per row (or reach the database and
fail as a 500). So these tests are mostly about REJECTING things.
"""

import dataclasses

import pytest
from pydantic import ValidationError

from app.parsers.generic_csv import ColumnMapping
from app.schemas.upload import ColumnMappingRequest, column_mapping_json_example

TWO_COLUMNS = {
    "date_column": "Date",
    "description_column": "Narration",
    "date_format": "%d/%m/%Y",
    "debit_column": "Withdrawal",
    "credit_column": "Deposit",
}
SIGNED_COLUMN = {
    "date_column": "Date",
    "description_column": "Narration",
    "date_format": "%d/%m/%Y",
    "amount_column": "Amount",
}


def message(exc: pytest.ExceptionInfo[ValidationError]) -> str:
    """All of a ValidationError's messages as one lowercase string."""
    return " ".join(error["msg"] for error in exc.value.errors()).lower()


# --- the two valid shapes -------------------------------------------------


def test_two_amount_columns_convert_to_the_dataclass() -> None:
    mapping = ColumnMappingRequest(**TWO_COLUMNS).to_mapping()

    assert isinstance(mapping, ColumnMapping)
    assert mapping.date_column == "Date"
    assert mapping.debit_column == "Withdrawal"
    assert mapping.credit_column == "Deposit"
    assert mapping.amount_column is None
    # Not given, so the parser will not look for a balance column.
    assert mapping.balance_column is None


def test_signed_amount_column_converts_to_the_dataclass() -> None:
    mapping = ColumnMappingRequest(**SIGNED_COLUMN).to_mapping()

    assert mapping.amount_column == "Amount"
    assert mapping.debit_column is None


def test_the_docs_example_is_a_valid_mapping() -> None:
    """The example shown in /docs must actually validate.

    An example that errors when pasted into "Try it out" is worse than no
    example, and nothing else would catch it.
    """
    assert ColumnMappingRequest(**column_mapping_json_example()).to_mapping() is not None


def test_labels_are_trimmed() -> None:
    """A header copied out of Excel often carries spaces around it."""
    mapping = ColumnMappingRequest(**{**TWO_COLUMNS, "date_column": "  Date  "}).to_mapping()

    assert mapping.date_column == "Date"


# --- the amount columns ---------------------------------------------------


def test_both_amount_styles_are_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        ColumnMappingRequest(**{**TWO_COLUMNS, "amount_column": "Amount"})

    assert "either" in message(exc)


def test_half_of_the_debit_credit_pair_is_rejected() -> None:
    """The mistake worth catching: with only a debit column, every credit
    row would silently be read as money going out."""
    with pytest.raises(ValidationError) as exc:
        ColumnMappingRequest(
            date_column="Date",
            description_column="Narration",
            date_format="%d/%m/%Y",
            debit_column="Withdrawal",
        )

    assert "together" in message(exc)


def test_no_amount_column_at_all_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        ColumnMappingRequest(
            date_column="Date", description_column="Narration", date_format="%d/%m/%Y"
        )

    assert "amount column is required" in message(exc)


def test_one_column_mapped_to_two_roles_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        ColumnMappingRequest(**{**TWO_COLUMNS, "credit_column": "withdrawal"})

    # Case- and space-insensitive, like the header matching itself.
    assert "only once" in message(exc)


# --- the date format ------------------------------------------------------


@pytest.mark.parametrize("date_format", ["%d/%m/%Y", "%d-%b-%Y", "%Y-%m-%d", "%m/%d/%Y"])
def test_valid_date_formats_are_accepted(date_format: str) -> None:
    assert ColumnMappingRequest(**{**TWO_COLUMNS, "date_format": date_format}).date_format


def test_a_format_without_a_year_is_rejected() -> None:
    """ "%d/%m" would read every date as the year 1900."""
    with pytest.raises(ValidationError) as exc:
        ColumnMappingRequest(**{**TWO_COLUMNS, "date_format": "%d/%m"})

    assert "day, month and year" in message(exc)


def test_a_format_that_is_not_a_date_format_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        ColumnMappingRequest(**{**TWO_COLUMNS, "date_format": "%q"})

    assert "valid date format" in message(exc)


def test_plain_text_is_not_a_date_format() -> None:
    with pytest.raises(ValidationError) as exc:
        ColumnMappingRequest(**{**TWO_COLUMNS, "date_format": "dd/mm/yyyy"})

    assert "day, month and year" in message(exc)


# --- the usual input validation -------------------------------------------


def test_unknown_fields_are_rejected() -> None:
    """extra="forbid": a typo like "date_col" must fail loudly, not be
    ignored and leave the real field missing."""
    with pytest.raises(ValidationError):
        ColumnMappingRequest(**{**TWO_COLUMNS, "date_col": "Date"})


def test_a_blank_column_name_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ColumnMappingRequest(**{**TWO_COLUMNS, "date_column": "   "})


def test_a_control_character_in_a_column_name_is_rejected() -> None:
    """A NUL cannot be stored by Postgres at all, and a newline is not a
    column header; both become a 422 rather than a 500."""
    with pytest.raises(ValidationError):
        ColumnMappingRequest(**{**TWO_COLUMNS, "description_column": "Narr\x00ation"})


def test_an_overlong_column_name_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ColumnMappingRequest(**{**TWO_COLUMNS, "description_column": "N" * 101})


def test_invalid_json_is_rejected() -> None:
    """How the route receives it: a string from a multipart form."""
    with pytest.raises(ValidationError):
        ColumnMappingRequest.model_validate_json("not json")


# --- the two classes must stay in step ------------------------------------


def test_the_schema_and_the_dataclass_have_the_same_fields() -> None:
    """The same idea is declared twice - once as a Pydantic model at the API
    edge, once as a frozen dataclass in the pure parser package (see the
    module docstring of app/schemas/upload.py). Adding a field to one and
    forgetting the other would silently drop it from every upload, so the
    two field lists are compared here rather than trusted.
    """
    dataclass_fields = {field.name for field in dataclasses.fields(ColumnMapping)}

    assert set(ColumnMappingRequest.model_fields) == dataclass_fields
