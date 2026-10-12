"""`TransactionFilters`: the query string of GET /transactions.

These are unit tests (no database, no HTTP): the model is where the rules
of that query live, so they can be checked directly. The matching
integration tests in tests/integration/test_transactions.py prove the same
rules produce a 422 through the real app.
"""

import datetime
import uuid
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.parsers.base import MAX_PAISE, Direction
from app.schemas.transaction import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    TransactionFilters,
    TransactionResponse,
)
from app.services.transactions import _search_pattern
from app.utils.cursor import encode_cursor

A_DATE = datetime.date(2026, 8, 4)
AN_ID = uuid.uuid4()


def filters(**params: Any) -> TransactionFilters:
    return TransactionFilters.model_validate(params)


def errors(**params: Any) -> list[tuple[str, str]]:
    """Validate and return the (field, message) pairs of the failure."""
    with pytest.raises(ValidationError) as exc_info:
        filters(**params)
    return [
        (".".join(str(part) for part in error["loc"]), error["msg"])
        for error in exc_info.value.errors()
    ]


# --- Defaults ---------------------------------------------------------------


def test_an_empty_query_filters_nothing() -> None:
    """Every filter is optional; only `limit` has a default."""
    result = filters()

    assert result.limit == DEFAULT_LIMIT
    assert result.date_from is None
    assert result.date_to is None
    assert result.q is None
    assert result.cursor is None
    assert result.cursor_key() is None
    assert result.min_amount_paise is None
    assert result.max_amount_paise is None


def test_from_and_to_are_read_from_their_spec_names() -> None:
    """`from` is a Python keyword, so the fields carry aliases."""
    result = filters(**{"from": "2026-08-01", "to": "2026-08-31"})

    assert result.date_from == datetime.date(2026, 8, 1)
    assert result.date_to == datetime.date(2026, 8, 31)
    # The Python name is not an accepted spelling, so there is exactly one.
    assert errors(date_from="2026-08-01") == [("date_from", "Extra inputs are not permitted")]


# --- Amounts ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("rupees", "paise"),
    [
        ("0", 0),
        ("1", 100),
        ("500.5", 50_050),
        ("1250.50", 125_050),
        # 0.29 is the classic float trap: 0.29 * 100 == 28.999999999999996,
        # so a float would store 28 paise. Decimal gives exactly 29.
        ("0.29", 29),
        ("125000", 12_500_000),
    ],
)
def test_amounts_are_converted_to_paise_exactly(rupees: str, paise: int) -> None:
    result = filters(min_amount=rupees, max_amount=rupees)

    assert result.min_amount_paise == paise
    assert result.max_amount_paise == paise


def test_an_amount_is_never_parsed_as_a_float() -> None:
    """A query parameter is a string, so Decimal gets it digit for digit."""
    result = filters(min_amount="0.29")

    assert result.min_amount == Decimal("0.29")
    assert isinstance(result.min_amount, Decimal)


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"min_amount": "-0.01"}, "min_amount"),
        ({"min_amount": "10.123"}, "min_amount"),
        ({"max_amount": "abc"}, "max_amount"),
        # Larger than any amount that can be stored (BIGINT paise).
        ({"max_amount": str(MAX_PAISE // 100 + 1)}, "max_amount"),
    ],
)
def test_impossible_amounts_are_rejected(params: dict[str, str], field: str) -> None:
    assert [name for name, _ in errors(**params)] == [field]


# --- Ranges -----------------------------------------------------------------


def test_a_backwards_date_range_is_rejected() -> None:
    assert errors(**{"from": "2026-09-01", "to": "2026-08-01"}) == [
        ("", "Value error, 'from' must not be later than 'to'")
    ]


def test_a_backwards_amount_range_is_rejected() -> None:
    assert errors(min_amount="900", max_amount="100") == [
        ("", "Value error, 'min_amount' must not be greater than 'max_amount'")
    ]


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-08-01", "to": "2026-08-01"},  # one single day
        {"min_amount": "100", "max_amount": "100"},  # one exact amount
        {"from": "2026-08-01"},  # open-ended
        {"to": "2026-08-01"},
        {"min_amount": "100"},
        {"max_amount": "100"},
    ],
)
def test_equal_and_open_ended_ranges_are_fine(params: dict[str, str]) -> None:
    assert filters(**params) is not None


# --- Search text ------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_a_blank_search_becomes_no_search(value: str) -> None:
    """A cleared search box must not turn into "match nothing"."""
    assert filters(q=value).q is None


def test_search_text_is_trimmed_and_bounded() -> None:
    assert filters(q="  zomato  ").q == "zomato"
    assert [name for name, _ in errors(q="x" * 101)] == ["q"]
    # A NUL byte cannot be stored or compared in Postgres text at all.
    assert [name for name, _ in errors(q="zo\u0000mato")] == ["q"]


@pytest.mark.parametrize(
    ("search", "pattern"),
    [
        ("zomato", "%zomato%"),
        # The LIKE metacharacters are escaped, so they match themselves.
        ("50%", "%50\\%%"),
        ("a_b", "%a\\_b%"),
        ("back\\slash", "%back\\\\slash%"),
    ],
)
def test_the_search_pattern_escapes_like_metacharacters(search: str, pattern: str) -> None:
    assert _search_pattern(search) == pattern


# --- Limit and cursor -------------------------------------------------------


@pytest.mark.parametrize("limit", [1, DEFAULT_LIMIT, MAX_LIMIT])
def test_allowed_page_sizes(limit: int) -> None:
    assert filters(limit=limit).limit == limit


@pytest.mark.parametrize("limit", [0, -1, MAX_LIMIT + 1])
def test_page_sizes_outside_the_range_are_rejected(limit: int) -> None:
    assert [name for name, _ in errors(limit=limit)] == ["limit"]


def test_a_cursor_is_kept_and_decoded() -> None:
    cursor = encode_cursor(A_DATE, AN_ID)

    result = filters(cursor=cursor)

    assert result.cursor == cursor
    assert result.cursor_key() == (A_DATE, AN_ID)


@pytest.mark.parametrize("cursor", ["", "   "])
def test_an_empty_cursor_means_the_first_page(cursor: str) -> None:
    assert filters(cursor=cursor).cursor is None


def test_a_broken_cursor_is_a_validation_error_naming_the_field() -> None:
    """The client gets a 422 on `cursor`, not a 500 from the service."""
    assert errors(cursor="not-a-cursor") == [
        (
            "cursor",
            "Value error, is not a valid cursor; send the next_cursor from the previous page",
        )
    ]


def test_an_enormous_cursor_is_rejected_before_being_decoded() -> None:
    assert [name for name, _ in errors(cursor="A" * 10_000)] == ["cursor"]


# --- Unknown parameters -----------------------------------------------------


def test_an_unknown_parameter_is_rejected() -> None:
    """A typo must not silently return everything."""
    assert [name for name, _ in errors(catgory_id=str(uuid.uuid4()))] == ["catgory_id"]


def test_direction_accepts_only_the_two_known_values() -> None:
    assert filters(direction="credit").direction is Direction.CREDIT
    assert [name for name, _ in errors(direction="sideways")] == ["direction"]


# --- The response -----------------------------------------------------------


def test_the_response_adds_an_indian_formatted_amount() -> None:
    """`amount_display` is derived from `amount_paise` (rules 1 and 10)."""
    response = TransactionResponse(
        id=AN_ID,
        account_id=AN_ID,
        upload_id=AN_ID,
        txn_date=A_DATE,
        value_date=None,
        amount_paise=12_500_000,
        direction=Direction.DEBIT,
        balance_after_paise=None,
        raw_description="UPI/ZOMATO/PAY",
        normalized_description="UPI/ZOMATO/PAY",
        channel="other",
        counterparty_vpa=None,
        category_id=None,
        category_source="none",
        category_confidence=None,
        is_self_transfer=False,
        notes=None,
        created_at=datetime.datetime(2026, 8, 4, 12, 0, tzinfo=datetime.UTC),
    )

    assert response.amount_display == "₹1,25,000.00"
    assert response.model_dump()["amount_display"] == "₹1,25,000.00"
