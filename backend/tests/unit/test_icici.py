"""Tests for app.parsers.icici, against a synthetic ICICI `.xls`.

Every test builds a real workbook with `tests/fixtures/icici_xls.py` (same
layout as the real download, invented data) and reads it through
`readers.read_xls`, so the test covers the path an upload actually takes:
bytes -> reader -> parser.

The expected numbers mirror the real file's shape and were worked out by
hand, which is the point: if the parser's arithmetic is wrong, the test must
disagree with it rather than repeat it.
"""

import datetime
from pathlib import Path

import pytest

from app.parsers.base import Direction, FileType, ParseError, ParserInput
from app.parsers.icici import IciciXlsParser
from app.parsers.readers import read_xls
from app.parsers.registry import build_default_registry
from tests.fixtures.icici_xls import DEFAULT_ROWS, HEADER_LABELS, build_icici_xls


def read(tmp_path: Path, **kwargs) -> ParserInput:
    """Build a synthetic statement and read it into rows, as an upload would."""
    path = build_icici_xls(tmp_path / "statement.xls", **kwargs)
    return read_xls(path.read_bytes())


def parse(tmp_path: Path, **kwargs):
    return IciciXlsParser().parse(read(tmp_path, **kwargs))


# --- detect ---------------------------------------------------------------


def test_detect_recognises_the_icici_header(tmp_path: Path) -> None:
    assert IciciXlsParser().detect(read(tmp_path)) == 0.95


def test_detect_returns_zero_for_another_bank(tmp_path: Path) -> None:
    """detect() must answer "not mine" without raising (the Protocol's rule)."""
    other_bank = ParserInput(
        file_type=FileType.XLS,
        rows=[["Txn Date", "Description", "Amount", "Closing Balance"]],
    )

    assert IciciXlsParser().detect(other_bank) == 0.0


def test_the_registry_picks_this_parser_for_an_icici_file(tmp_path: Path) -> None:
    parser = build_default_registry().detect(read(tmp_path))

    assert parser is not None
    assert parser.bank_code == "ICICI"


def test_parser_metadata_matches_the_account_bank_codes() -> None:
    """`bank_code` must be one of the values `accounts.bank_code` allows."""
    assert IciciXlsParser().bank_code == "ICICI"
    assert IciciXlsParser().file_types == (FileType.XLS,)


# --- the happy path -------------------------------------------------------


def test_all_transaction_rows_are_read(tmp_path: Path) -> None:
    """DEFAULT_ROWS has six rows, one of which only continues a narration."""
    statement = parse(tmp_path)

    assert len(statement.rows) == 5


def test_the_first_row_is_read_correctly(tmp_path: Path) -> None:
    row = parse(tmp_path).rows[0]

    assert row.txn_date == datetime.date(2026, 8, 1)
    assert row.value_date == datetime.date(2026, 8, 1)
    assert row.amount_paise == 7000
    assert row.direction is Direction.DEBIT
    assert row.raw_description == "UPI/TESTSHOP/PAY"
    assert row.balance_after_paise == 11147133


def test_a_deposit_row_is_a_credit(tmp_path: Path) -> None:
    salary = parse(tmp_path).rows[3]

    assert (salary.amount_paise, salary.direction) == (5000000, Direction.CREDIT)


def test_row_numbers_point_at_the_row_of_the_file(tmp_path: Path) -> None:
    """The header is file row 13 (xlrd row 12), so data starts at row 14."""
    assert [row.row_number for row in parse(tmp_path).rows] == [14, 15, 16, 17, 18]


def test_two_identical_rows_are_both_kept(tmp_path: Path) -> None:
    """Two ₹20 chai payments on one day are two transactions, not one.
    (The fingerprint keeps them apart; see ADR 006.)"""
    rows = parse(tmp_path).rows

    assert rows[1].amount_paise == rows[2].amount_paise == 2000
    assert rows[1].txn_date == rows[2].txn_date


def test_the_statement_period_comes_from_the_header(tmp_path: Path) -> None:
    statement = parse(tmp_path)

    assert statement.period_start == datetime.date(2026, 8, 1)
    assert statement.period_end == datetime.date(2026, 8, 31)


def test_a_period_of_na_is_reported_as_unknown(tmp_path: Path) -> None:
    """ICICI writes "NA" in several header fields; that must not fail the
    import, because the rows still carry their own dates."""
    statement = parse(tmp_path, period_from="NA", period_to="NA")

    assert (statement.period_start, statement.period_end) == (None, None)
    assert statement.row_date_range == (datetime.date(2026, 8, 1), datetime.date(2026, 8, 4))


def test_only_the_last_four_digits_of_the_account_are_returned(tmp_path: Path) -> None:
    """SPEC §7.3: nothing more than four digits may leave the parser, and the
    holder's name in the same cell must be dropped."""
    statement = parse(tmp_path, account_number="084612349876", holder="SOME NAME")

    assert statement.account_last4 == "9876"


def test_a_missing_account_header_is_not_an_error(tmp_path: Path) -> None:
    statement = parse(tmp_path, account_number="", holder="")

    assert statement.account_last4 is None


# --- continuation rows ----------------------------------------------------


def test_a_split_narration_is_joined_with_nothing_in_between(tmp_path: Path) -> None:
    """The bank cuts a long narration mid-token and spills the rest into the
    next row. Joining with a space would invent a word boundary - and because
    the normalized description feeds the dedupe fingerprint (ADR 006), that
    would change the transaction's identity."""
    row = parse(tmp_path).rows[4]

    assert row.raw_description == "MMT/IMPS/ACME BANK LTD/205412345678/PAYMENT"


def test_a_continuation_row_is_not_a_transaction(tmp_path: Path) -> None:
    rows = parse(tmp_path).rows

    assert [row.amount_paise for row in rows] == [7000, 2000, 2000, 5000000, 150000]


def test_two_continuation_rows_in_a_row_are_both_joined(tmp_path: Path) -> None:
    rows = (
        ("1", "01/08/2026", "01/08/2026", "", "UPI/SHOP", "70.00", "0.00", "111471.33"),
        ("", "", "", "", "/PART2", "", "", ""),
        ("", "", "", "", "/PART3", "", "", ""),
    )

    statement = parse(tmp_path, rows=rows)

    assert statement.rows[0].raw_description == "UPI/SHOP/PART2/PART3"


# --- derived balances -----------------------------------------------------


def test_opening_and_closing_balances_are_derived_from_the_rows(tmp_path: Path) -> None:
    """ICICI prints neither. The first row is a ₹70 debit leaving
    ₹1,11,471.33, so the account held ₹1,11,541.33 before it; the closing
    balance is simply the last row's balance."""
    statement = parse(tmp_path)

    assert statement.opening_balance_paise == 11154133
    assert statement.closing_balance_paise == 15993133


def test_the_derived_balances_agree_with_the_rows(tmp_path: Path) -> None:
    """opening - debits + credits == closing, in exact integer paise."""
    statement = parse(tmp_path)
    debits = sum(r.amount_paise for r in statement.rows if r.direction is Direction.DEBIT)
    credits = sum(r.amount_paise for r in statement.rows if r.direction is Direction.CREDIT)

    assert statement.opening_balance_paise - debits + credits == statement.closing_balance_paise


def test_a_credit_as_the_first_row_is_undone_the_other_way(tmp_path: Path) -> None:
    rows = (("1", "03/08/2026", "03/08/2026", "", "SALARY", "0.00", "50000.00", "161471.33"),)

    statement = parse(tmp_path, rows=rows)

    assert statement.opening_balance_paise == 11147133  # 161471.33 - 50000.00
    assert statement.closing_balance_paise == 16147133


def test_blank_balance_cells_mean_no_derived_balances(tmp_path: Path) -> None:
    """Nothing is invented when there is no balance to work from."""
    rows = (
        ("1", "01/08/2026", "01/08/2026", "", "UPI/SHOP", "70.00", "0.00", ""),
        ("2", "02/08/2026", "02/08/2026", "", "UPI/CHAI", "20.00", "0.00", ""),
    )

    statement = parse(tmp_path, rows=rows)

    assert statement.rows[0].balance_after_paise is None
    assert statement.opening_balance_paise is None
    assert statement.closing_balance_paise is None


def test_a_statement_without_a_balance_column_is_not_an_icici_statement(tmp_path: Path) -> None:
    """Every real ICICI statement has one, so a missing Balance column means
    the file is some other layout - better refused than half-read."""
    labels = tuple(label for label in HEADER_LABELS if label != "Balance(INR)")
    rows = (("1", "01/08/2026", "01/08/2026", "", "UPI/SHOP", "70.00", "0.00"),)

    with pytest.raises(ParseError, match="does not look like an ICICI statement"):
        parse(tmp_path, rows=rows, header_labels=labels)


# --- where the table ends -------------------------------------------------


def test_the_legend_block_is_not_read_as_transactions(tmp_path: Path) -> None:
    """ICICI appends 27 legend lines after the last row."""
    statement = parse(tmp_path, with_legend=True)

    assert len(statement.rows) == 5


def test_a_file_without_a_legend_block_is_fine_too(tmp_path: Path) -> None:
    statement = parse(tmp_path, with_legend=False)

    assert len(statement.rows) == 5


def test_rows_after_a_blank_row_are_not_read(tmp_path: Path) -> None:
    rows = (
        ("1", "01/08/2026", "01/08/2026", "", "UPI/SHOP", "70.00", "0.00", "111471.33"),
        ("", "", "", "", "", "", "", ""),
        ("2", "02/08/2026", "02/08/2026", "", "UPI/CHAI", "20.00", "0.00", "111451.33"),
    )

    statement = parse(tmp_path, rows=rows)

    assert len(statement.rows) == 1


# --- errors ---------------------------------------------------------------


def test_a_file_without_the_icici_header_is_rejected(tmp_path: Path) -> None:
    labels = ("Sr", "Date", "Details", "Debit", "Credit", "Closing")

    with pytest.raises(ParseError, match="does not look like an ICICI statement"):
        parse(tmp_path, header_labels=labels)


def test_a_statement_with_no_transaction_rows_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ParseError, match="no transaction rows"):
        parse(tmp_path, rows=())


def test_a_row_with_both_a_withdrawal_and_a_deposit_is_rejected(tmp_path: Path) -> None:
    rows = (("1", "01/08/2026", "01/08/2026", "", "UPI/SHOP", "70.00", "50.00", "111471.33"),)

    with pytest.raises(ParseError) as exc_info:
        parse(tmp_path, rows=rows)

    assert exc_info.value.row_number == 14
    assert "both a withdrawal and a deposit" in exc_info.value.reason


def test_a_row_with_neither_amount_is_rejected(tmp_path: Path) -> None:
    rows = (("1", "01/08/2026", "01/08/2026", "", "UPI/SHOP", "0.00", "0.00", "111471.33"),)

    with pytest.raises(ParseError, match="neither a withdrawal nor a deposit"):
        parse(tmp_path, rows=rows)


def test_a_broken_date_fails_loudly_instead_of_ending_the_table(tmp_path: Path) -> None:
    """The important half of the end-of-table rule: a row that HAS a date
    cell must never be silently skipped, or one odd row would drop the rest
    of the month and make every total wrong."""
    rows = (
        ("1", "01/08/2026", "01/08/2026", "", "UPI/SHOP", "70.00", "0.00", "111471.33"),
        ("2", "02/08/2026", "32/13/2026", "", "UPI/CHAI", "20.00", "0.00", "111451.33"),
    )

    with pytest.raises(ParseError) as exc_info:
        parse(tmp_path, rows=rows)

    assert exc_info.value.row_number == 15
    assert exc_info.value.reason == "transaction date is not a dd/mm/yyyy date"


def test_an_unreadable_amount_is_reported_with_its_row(tmp_path: Path) -> None:
    rows = (("1", "01/08/2026", "01/08/2026", "", "UPI/SHOP", "seventy", "0.00", "111471.33"),)

    with pytest.raises(ParseError) as exc_info:
        parse(tmp_path, rows=rows)

    assert exc_info.value.row_number == 14
    assert exc_info.value.reason == "withdrawal is not a valid amount"


def test_a_non_numeric_serial_number_is_rejected(tmp_path: Path) -> None:
    """A dated row whose S No. is not a number is not a transaction row."""
    rows = (("Total", "01/08/2026", "01/08/2026", "", "UPI/SHOP", "70.00", "0.00", "111471.33"),)

    with pytest.raises(ParseError, match="serial number column is not a number"):
        parse(tmp_path, rows=rows)


def test_an_error_message_never_contains_the_statement_data(tmp_path: Path) -> None:
    """CLAUDE.md rule 3: errors are logged, so they carry no narration."""
    rows = (
        ("1", "01/08/2026", "01/08/2026", "", "UPI/SECRETSHOP/9988", "rubbish", "0.00", "11.33"),
    )

    with pytest.raises(ParseError) as exc_info:
        parse(tmp_path, rows=rows)

    assert "SECRETSHOP" not in str(exc_info.value)
    assert "9988" not in str(exc_info.value)


# --- robustness against layout changes ------------------------------------


def test_the_table_may_start_in_any_column(tmp_path: Path) -> None:
    """Nothing is read by column position: the real file leaves column 0
    empty, and the parser finds its columns by their header labels."""
    statement = parse(tmp_path)

    assert statement.rows[0].amount_paise == 7000


def test_an_inr_suffix_change_still_matches(tmp_path: Path) -> None:
    """Labels are matched on their start, so spacing changes are harmless."""
    labels = (
        "S No.",
        "Value Date",
        "Transaction Date",
        "Cheque Number",
        "Transaction Remarks",
        "Withdrawal Amount (INR)",
        "Deposit Amount (INR)",
        "Balance (INR)",
    )

    statement = parse(tmp_path, header_labels=labels)

    assert len(statement.rows) == len(DEFAULT_ROWS) - 1


def test_a_footer_row_without_a_date_ends_the_table(tmp_path: Path) -> None:
    """Not every footer says "Legends": a total row has no date and no
    narration, so it ends the table too."""
    rows = (
        ("1", "01/08/2026", "01/08/2026", "", "UPI/SHOP", "70.00", "0.00", "111471.33"),
        ("", "", "", "", "", "", "", "111471.33"),
        ("2", "02/08/2026", "02/08/2026", "", "UPI/CHAI", "20.00", "0.00", "111451.33"),
    )

    statement = parse(tmp_path, rows=rows)

    assert len(statement.rows) == 1


def test_a_missing_period_row_is_not_an_error(tmp_path: Path) -> None:
    statement = parse(tmp_path, with_period=False)

    assert (statement.period_start, statement.period_end) == (None, None)
    assert len(statement.rows) == 5
