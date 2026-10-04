"""Tests for app.parsers.generic_csv: the user-mapped CSV parser."""

import datetime

import pytest

from app.parsers.base import Direction, FileType, ParseError, ParserInput
from app.parsers.generic_csv import ColumnMapping, GenericCsvParser
from app.parsers.readers import read_csv

# The two-column style: one column for money out, one for money in.
PAIR_MAPPING = ColumnMapping(
    date_column="Date",
    description_column="Narration",
    date_format="%d/%m/%Y",
    debit_column="Withdrawal",
    credit_column="Deposit",
    balance_column="Balance",
)

HEADER = "Date,Narration,Withdrawal,Deposit,Balance\n"


def parse(csv_text: str, mapping: ColumnMapping = PAIR_MAPPING):
    """Read CSV text the way an upload would, then parse it."""
    return GenericCsvParser(mapping).parse(read_csv(csv_text.encode()))


# --- ColumnMapping validation ---------------------------------------------


def test_mapping_needs_an_amount_column() -> None:
    with pytest.raises(ValueError, match="an amount column is required"):
        ColumnMapping(date_column="Date", description_column="Narration", date_format="%d/%m/%Y")


def test_mapping_rejects_both_amount_styles_at_once() -> None:
    with pytest.raises(ValueError, match="either debit_column and credit_column"):
        ColumnMapping(
            date_column="Date",
            description_column="Narration",
            date_format="%d/%m/%Y",
            debit_column="Withdrawal",
            credit_column="Deposit",
            amount_column="Amount",
        )


def test_mapping_rejects_half_of_the_pair() -> None:
    """Debit alone would read every credit as a debit and lose all income."""
    with pytest.raises(ValueError, match="must be given together"):
        ColumnMapping(
            date_column="Date",
            description_column="Narration",
            date_format="%d/%m/%Y",
            debit_column="Withdrawal",
        )


@pytest.mark.parametrize("missing", ["date_column", "description_column", "date_format"])
def test_mapping_rejects_a_blank_required_field(missing: str) -> None:
    fields = {
        "date_column": "Date",
        "description_column": "Narration",
        "date_format": "%d/%m/%Y",
        "amount_column": "Amount",
    }
    fields[missing] = "   "

    with pytest.raises(ValueError, match=f"{missing} is required"):
        ColumnMapping(**fields)


def test_required_columns_lists_every_mapped_column() -> None:
    assert PAIR_MAPPING.required_columns == (
        "Date",
        "Narration",
        "Withdrawal",
        "Deposit",
        "Balance",
    )


# --- the happy path -------------------------------------------------------


def test_a_debit_and_a_credit_row_are_read_correctly() -> None:
    statement = parse(
        HEADER + "01/08/2026,UPI/SHOP,70.00,,111471.33\n03/08/2026,SALARY,,50000.00,161471.33\n"
    )

    debit, credit = statement.rows
    assert (debit.txn_date, debit.amount_paise, debit.direction) == (
        datetime.date(2026, 8, 1),
        7000,
        Direction.DEBIT,
    )
    assert debit.raw_description == "UPI/SHOP"
    assert debit.balance_after_paise == 11147133
    assert (credit.amount_paise, credit.direction) == (5000000, Direction.CREDIT)


def test_row_numbers_point_at_the_line_of_the_file() -> None:
    """So an error message sends the user to the right row in Excel."""
    statement = parse(HEADER + "01/08/2026,UPI/SHOP,70.00,,111471.33\n")

    # Line 1 is the header, so the first transaction is line 2.
    assert statement.rows[0].row_number == 2


def test_the_header_is_found_below_title_rows() -> None:
    """Banks print a title and an account summary above the table."""
    statement = parse(
        "MY BANK LTD\nAccount: 1234\n\n" + HEADER + "01/08/2026,UPI/SHOP,70.00,,111471.33\n"
    )

    assert len(statement.rows) == 1
    assert statement.rows[0].row_number == 5


def test_header_matching_ignores_case_and_padding() -> None:
    statement = parse(
        " DATE , narration , Withdrawal , DEPOSIT , balance \n"
        "01/08/2026,UPI/SHOP,70.00,,111471.33\n"
    )

    assert statement.rows[0].amount_paise == 7000


def test_blank_rows_and_footer_rows_are_skipped() -> None:
    """A separator line or a "Total" footer has no date, so it is not a row."""
    statement = parse(
        HEADER
        + "01/08/2026,UPI/SHOP,70.00,,111471.33\n"
        + "\n"
        + ",Total,70.00,,\n"
        + "02/08/2026,UPI/CHAI,20.00,,111451.33\n"
    )

    assert [row.row_number for row in statement.rows] == [2, 5]


def test_zero_in_the_unused_column_counts_as_empty() -> None:
    """Many banks print 0.00 instead of leaving the other side blank."""
    statement = parse(HEADER + "01/08/2026,UPI/SHOP,70.00,0.00,111471.33\n")

    assert statement.rows[0].direction is Direction.DEBIT


def test_commas_and_a_rupee_sign_in_an_amount_are_handled() -> None:
    statement = parse(HEADER + '01/08/2026,UPI/SHOP,"₹1,250.50",,111471.33\n')

    assert statement.rows[0].amount_paise == 125050


def test_a_dr_suffix_does_not_flip_the_direction_twice() -> None:
    """The column already says "money out"; the suffix must not negate it."""
    statement = parse(HEADER + "01/08/2026,UPI/SHOP,70.00 Dr,,111471.33\n")

    row = statement.rows[0]
    assert (row.amount_paise, row.direction) == (7000, Direction.DEBIT)


def test_a_negative_balance_is_kept_as_is() -> None:
    """An overdrawn account is real; only the amount must be positive."""
    statement = parse(HEADER + "01/08/2026,UPI/SHOP,70.00,,-500.00\n")

    assert statement.rows[0].balance_after_paise == -50000


def test_the_balance_column_is_optional() -> None:
    mapping = ColumnMapping(
        date_column="Date",
        description_column="Narration",
        date_format="%d/%m/%Y",
        debit_column="Withdrawal",
        credit_column="Deposit",
    )

    statement = parse("Date,Narration,Withdrawal,Deposit\n01/08/2026,UPI/SHOP,70.00,\n", mapping)

    assert statement.rows[0].balance_after_paise is None


def test_a_bare_csv_reports_no_period_but_a_date_range() -> None:
    """Nothing is invented: a CSV states no period, so Step 7 uses the rows."""
    statement = parse(HEADER + "01/08/2026,UPI/SHOP,70.00,,1.00\n05/08/2026,UPI/CHAI,20.00,,2.00\n")

    assert (statement.period_start, statement.period_end) == (None, None)
    assert statement.row_date_range == (datetime.date(2026, 8, 1), datetime.date(2026, 8, 5))


def test_another_date_format_is_honoured() -> None:
    mapping = ColumnMapping(
        date_column="Date",
        description_column="Narration",
        date_format="%d-%b-%Y",
        debit_column="Withdrawal",
        credit_column="Deposit",
    )

    statement = parse("Date,Narration,Withdrawal,Deposit\n01-Aug-2026,UPI/SHOP,70.00,\n", mapping)

    assert statement.rows[0].txn_date == datetime.date(2026, 8, 1)


# --- errors ---------------------------------------------------------------


def test_a_missing_mapped_column_is_reported_by_name() -> None:
    with pytest.raises(ParseError, match="Balance"):
        parse("Date,Narration,Withdrawal,Deposit\n01/08/2026,UPI/SHOP,70.00,\n")


def test_a_date_that_does_not_match_the_format_fails_with_its_row_number() -> None:
    with pytest.raises(ParseError) as exc_info:
        parse(HEADER + "2026-08-01,UPI/SHOP,70.00,,111471.33\n")

    assert exc_info.value.row_number == 2
    assert "%d/%m/%Y" in exc_info.value.reason


def test_a_row_with_both_a_debit_and_a_credit_is_rejected() -> None:
    with pytest.raises(ParseError, match="both a debit and a credit"):
        parse(HEADER + "01/08/2026,UPI/SHOP,70.00,50.00,111471.33\n")


def test_a_row_with_a_date_but_no_amount_is_rejected() -> None:
    """Skipping it silently would make every total wrong without a trace."""
    with pytest.raises(ParseError, match="no amount"):
        parse(HEADER + "01/08/2026,UPI/SHOP,,,111471.33\n")


def test_an_unreadable_amount_is_reported_with_its_column_and_row() -> None:
    with pytest.raises(ParseError) as exc_info:
        parse(HEADER + "01/08/2026,UPI/SHOP,seventy,,111471.33\n")

    assert exc_info.value.row_number == 2
    assert exc_info.value.reason == "debit is not a valid amount"


def test_an_error_message_never_contains_the_statement_data() -> None:
    """CLAUDE.md rule 3: no narrations or amounts in errors (they get logged)."""
    with pytest.raises(ParseError) as exc_info:
        parse(HEADER + "01/08/2026,SECRET MERCHANT,1234.56 rubbish,,111471.33\n")

    message = str(exc_info.value)
    assert "SECRET MERCHANT" not in message
    assert "1234.56" not in message


# --- detection ------------------------------------------------------------


def test_this_parser_never_claims_a_file() -> None:
    """It cannot work without the user's mapping, so the registry must not
    pick it; the import service (Step 7) chooses it explicitly."""
    parser = GenericCsvParser(PAIR_MAPPING)

    assert parser.detect(ParserInput(file_type=FileType.CSV, rows=[["Date", "Narration"]])) == 0.0
    assert parser.bank_code == "GENERIC"
    assert parser.file_types == (FileType.CSV,)


# --- the signed-amount style: TODO(dhruv) ---------------------------------


def test_a_signed_amount_column_is_not_supported_yet() -> None:
    """Remove this test when the TODO(dhruv) branch below is implemented."""
    mapping = ColumnMapping(
        date_column="Date",
        description_column="Narration",
        date_format="%d/%m/%Y",
        amount_column="Amount",
    )

    with pytest.raises(ParseError, match="not supported yet"):
        parse("Date,Narration,Amount\n01/08/2026,UPI/SHOP,-70.00\n", mapping)


SIGNED_MAPPING = ColumnMapping(
    date_column="Date",
    description_column="Narration",
    date_format="%d/%m/%Y",
    amount_column="Amount",
    balance_column="Balance",
)

SIGNED_HEADER = "Date,Narration,Amount,Balance\n"


@pytest.mark.skip(reason="TODO(dhruv): implement the signed-amount branch in generic_csv.py")
def test_signed_amount_column_negative_is_a_debit() -> None:
    statement = parse(SIGNED_HEADER + "01/08/2026,UPI/SHOP,-70.00,111471.33\n", SIGNED_MAPPING)

    row = statement.rows[0]
    assert (row.amount_paise, row.direction) == (7000, Direction.DEBIT)


@pytest.mark.skip(reason="TODO(dhruv): implement the signed-amount branch in generic_csv.py")
def test_signed_amount_column_positive_is_a_credit() -> None:
    statement = parse(SIGNED_HEADER + "03/08/2026,SALARY,50000.00,161471.33\n", SIGNED_MAPPING)

    row = statement.rows[0]
    assert (row.amount_paise, row.direction) == (5000000, Direction.CREDIT)


@pytest.mark.skip(reason="TODO(dhruv): implement the signed-amount branch in generic_csv.py")
def test_signed_amount_column_rejects_an_empty_amount() -> None:
    with pytest.raises(ParseError, match="no amount"):
        parse(SIGNED_HEADER + "01/08/2026,UPI/SHOP,,111471.33\n", SIGNED_MAPPING)
