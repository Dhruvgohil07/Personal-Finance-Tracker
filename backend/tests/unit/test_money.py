"""Tests for app.utils.money: exact paise parsing and INR formatting."""

import pytest

from app.utils.money import MoneyParseError, format_inr, parse_amount_to_paise

# --- parse_amount_to_paise -----------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Plain numbers and decimal places
        ("1250", 125000),
        ("1250.5", 125050),
        ("1250.50", 125050),
        ("0.01", 1),
        ("0", 0),
        # Commas: Indian and Western grouping both work
        ("1,25,000.00", 12500000),
        ("125,000.00", 12500000),
        # Surrounding whitespace
        ("  1,250.00  ", 125000),
        # Currency prefixes
        ("₹1,250.00", 125000),
        ("₹ 1,250.00", 125000),
        ("Rs. 1,250.00", 125000),
        ("INR 1250", 125000),
        # Cr / Dr suffixes, any case, with or without space and dot
        ("1,250.00 Cr", 125000),
        ("1,250.00 Dr", -125000),
        ("1250.00CR", 125000),
        ("1250.00 dr.", -125000),
        # Bracketed negatives and explicit signs
        ("(1,250.00)", -125000),
        ("-1,250.00", -125000),
        ("+1,250.00", 125000),
        ("-₹1,250.00", -125000),
    ],
)
def test_parse_valid_amounts(text: str, expected: int) -> None:
    assert parse_amount_to_paise(text) == expected


@pytest.mark.parametrize("text", [None, "", "   ", "\t"])
def test_blank_cell_is_none(text: str | None) -> None:
    assert parse_amount_to_paise(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "abc",
        "12.345",  # 3 decimal places: rounding would silently change the amount
        "12.",
        "1.2.3",
        "NaN",  # Decimal() would accept these three; we must not
        "Infinity",
        "1e5",
        "Dr",  # a suffix with no number
        "(100) Cr",  # two sign markers that disagree
        "-100 Dr",
        "-(100)",
    ],
)
def test_parse_invalid_amounts_raise(text: str) -> None:
    with pytest.raises(MoneyParseError):
        parse_amount_to_paise(text)


def test_parse_error_message_does_not_contain_the_amount() -> None:
    with pytest.raises(MoneyParseError) as exc_info:
        parse_amount_to_paise("98,765.432")

    assert "98" not in str(exc_info.value)


def test_parse_is_exact_where_float_would_be_wrong() -> None:
    # The classic float bug: int(0.29 * 100) == 28. Decimal gives 29.
    assert int(float("0.29") * 100) == 28  # proves the trap is real
    assert parse_amount_to_paise("0.29") == 29


def test_parse_returns_int_not_float_or_decimal() -> None:
    assert type(parse_amount_to_paise("1,250.50")) is int


# --- format_inr ----------------------------------------------------------


@pytest.mark.parametrize(
    ("paise", "expected"),
    [
        (0, "₹0.00"),
        (1, "₹0.01"),
        (50, "₹0.50"),
        (100, "₹1.00"),
        (99999, "₹999.99"),  # largest amount with no comma
        (100000, "₹1,000.00"),  # first comma after 3 digits
        (12500000, "₹1,25,000.00"),  # 1.25 lakh
        (1000000000, "₹1,00,00,000.00"),  # 1 crore
        (-125050, "-₹1,250.50"),
    ],
)
def test_format_inr(paise: int, expected: str) -> None:
    assert format_inr(paise) == expected


@pytest.mark.parametrize("value", [12.5, True, "1250"])
def test_format_rejects_non_integers(value: object) -> None:
    with pytest.raises(TypeError):
        format_inr(value)  # type: ignore[arg-type]


def test_parse_then_format_round_trip() -> None:
    assert format_inr(parse_amount_to_paise("1,25,000.50 Cr")) == "₹1,25,000.50"


def test_currency_symbol_inside_brackets_is_negative() -> None:
    assert parse_amount_to_paise("(₹ 1,250.00)") == -125000


def test_format_keeps_lakh_grouping_beyond_one_crore() -> None:
    assert format_inr(100000000000) == "₹1,00,00,00,000.00"


def test_format_keeps_minus_sign_when_rupees_are_zero() -> None:
    assert format_inr(-1) == "-₹0.01"
