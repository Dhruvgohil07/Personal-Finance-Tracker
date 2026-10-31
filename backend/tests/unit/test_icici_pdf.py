"""Tests for app.parsers.icici_pdf, on hand-built words (no PDF needed).

The parser only sees `PdfWord`s, so a statement can be laid out by hand at
the positions measured on the real ICICI PDF (ADR 019): the columns, the
dated line, remarks starting 5 pt above it and running down 10 pt apart.
Real PDFs, built with fpdf2, are read end to end in tests/integration/
test_pdf_imports.py.

Every name, VPA and number below is invented (CLAUDE.md rule 8).
"""

from datetime import date

import pytest

from app.parsers.base import Direction, FileType, ParseError, ParserInput, PdfWord
from app.parsers.icici_pdf import (
    IciciPdfParser,
    _find_account_last4,
    _find_period,
    _paise,
)

# The real file's column positions (measured, ADR 019).
REMARKS_LEFT = 185.58  # right edge of "Number"
EDGE = 398.98  # the Withdrawal column starts here
DEPOSIT_X = 473.79
BALANCE_X = 532.14
REMARKS_X = 192.0
CHAR = 5.0  # a made-up but consistent character width

HEADER_TOP = 210.0
FIRST_ROW = 250.0

PARSER = IciciPdfParser()


def word(text: str, page: int, x0: float, top: float, x1: float | None = None) -> PdfWord:
    right = x0 + CHAR * len(text) if x1 is None else x1
    return PdfWord(text=text, page=page, x0=x0, x1=right, top=top, bottom=top + 8)


def header(page: int) -> list[PdfWord]:
    """The column header, with the labels the parser locates columns by."""
    return [
        word("Cheque", page, 150.0, HEADER_TOP - 10),
        word("Number", page, 150.0, HEADER_TOP, x1=REMARKS_LEFT),
        word("Transaction", page, 260.0, HEADER_TOP - 10),
        word("Remarks", page, 260.0, HEADER_TOP),
        word("Withdrawal", page, EDGE, HEADER_TOP - 10),
        word("Deposit", page, DEPOSIT_X, HEADER_TOP - 10),
        word("Balance", page, BALANCE_X, HEADER_TOP - 10),
        word("(INR)", page, BALANCE_X, HEADER_TOP + 10),
    ]


def remark_line(text: str, page: int, top: float, *, full: bool = False) -> list[PdfWord]:
    """One remark line. `full` = filled to the bank's wrap edge (14 pt short
    of the Withdrawal column, as measured)."""
    words, x = [], REMARKS_X
    parts = text.split(" ")
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        x1 = EDGE - 14.0 if (full and last) else None
        words.append(word(part, page, x, top, x1=x1))
        x = words[-1].x1 + CHAR
    return words


def row(
    page: int,
    top: float,
    *,
    serial: str | None = "1",
    day: str = "01.08.2026",
    amount: str = "120.00",
    side: str = "withdrawal",
    balance: str = "99880.00",
    remarks: tuple[tuple[str, bool], ...] = (),
) -> list[PdfWord]:
    """A transaction: its dated line plus its remark lines (text, full?)."""
    amount_x = EDGE + 30 if side == "withdrawal" else DEPOSIT_X + 20
    if side == "balance":
        amount_x = BALANCE_X + 5
    words = [
        word(day, page, 61.4, top),
        word(amount, page, amount_x, top),
        word(balance, page, BALANCE_X + 3, top),
    ]
    if serial is not None:
        words.append(word(serial, page, 30.1, top))
    for index, (text, full) in enumerate(remarks):
        words.extend(remark_line(text, page, top - 5 + 10 * index, full=full))
    return words


def title_line(text: str) -> list[PdfWord]:
    """Page 1's title, drawn above the column header like the real one."""
    words, x = [], 30.0
    for part in text.split(" "):
        words.append(word(part, 1, x, 120.0))
        x = words[-1].x1 + CHAR
    return words


def statement(*words: list[PdfWord], title: str | None = None) -> ParserInput:
    groups = [title_line(title), *words] if title is not None else list(words)
    flat = [w for group in groups for w in group]
    rows = [[title]] if title is not None else []
    return ParserInput(FileType.PDF, rows=rows, pdf_words=flat)


# A UPI row laid out the way the bank does it: the name line, then the
# narration wrapped at spaces, and its 35-character txn id split mid-token.
UPI_REMARKS = (
    ("MOON CAFE", False),  # the name line: repeats the UPI name field
    ("UPI/MOON CAFE/q123456789@ybl/NO", False),
    ("REMARK/YES BANK", False),
    ("L/512345678901/ICI4f2a9c1e7b3d5f6a8c0e2b", True),  # split inside the txn id
    ("4d6f8a1c3e", False),
)
UPI_NARRATION = (
    "UPI/MOON CAFE/q123456789@ybl/NO REMARK/YES BANK L/512345678901/"
    "ICI4f2a9c1e7b3d5f6a8c0e2b4d6f8a1c3e"
)


# --- One row ----------------------------------------------------------------------


def test_a_upi_row_is_rebuilt_exactly_as_the_xls_prints_it() -> None:
    data = statement(header(1), row(1, FIRST_ROW, remarks=UPI_REMARKS))

    [parsed] = PARSER.parse(data).rows

    assert parsed.raw_description == UPI_NARRATION
    assert (parsed.txn_date, parsed.amount_paise, parsed.direction) == (
        date(2026, 8, 1),
        12000,
        Direction.DEBIT,
    )
    assert parsed.balance_after_paise == 9988000
    assert parsed.row_number == 1


def test_a_short_one_word_line_broke_at_a_space() -> None:
    """ "BANK" alone on a line: the next token did not fit, so a space was lost."""
    remarks = (("UPI/X/a@b/NO", False), ("BANK", False), ("L/1/2", False))

    [parsed] = PARSER.parse(statement(header(1), row(1, FIRST_ROW, remarks=remarks))).rows

    assert parsed.raw_description == "UPI/X/a@b/NO BANK L/1/2"


def test_a_full_line_of_several_words_still_broke_at_a_space() -> None:
    """Only a ONE-word full line can be a token split mid-way."""
    remarks = (("UPI/A B/c@d/NO", True), ("REMARK", False))

    [parsed] = PARSER.parse(statement(header(1), row(1, FIRST_ROW, remarks=remarks))).rows

    assert parsed.raw_description == "UPI/A B/c@d/NO REMARK"


def test_the_name_line_is_dropped_when_the_name_field_ends_in_a_space() -> None:
    """The bank cut the name right after a space: the field is "RAM KUMAR "
    (the next word starts with "/"), the name line "RAM KUMAR" (review)."""
    remarks = (("RAM KUMAR", False), ("UPI/RAM KUMAR /q12@ybl/NO", False))

    [parsed] = PARSER.parse(statement(header(1), row(1, FIRST_ROW, remarks=remarks))).rows

    assert parsed.raw_description == "UPI/RAM KUMAR /q12@ybl/NO"


def test_a_first_line_that_is_not_the_upi_name_is_kept() -> None:
    """Dropped only when it repeats the UPI name field (decided with Dhruv)."""
    remarks = (("ACME CORP", False), ("NEFT-SALARY-ACME", False))

    [parsed] = PARSER.parse(statement(header(1), row(1, FIRST_ROW, remarks=remarks))).rows

    assert parsed.raw_description == "ACME CORP NEFT-SALARY-ACME"


def test_a_one_line_remark_is_kept_as_it_is() -> None:
    [parsed] = PARSER.parse(
        statement(header(1), row(1, FIRST_ROW, remarks=(("ATM CASH", False),)))
    ).rows

    assert parsed.raw_description == "ATM CASH"


def test_a_row_without_remarks_has_an_empty_narration() -> None:
    [parsed] = PARSER.parse(statement(header(1), row(1, FIRST_ROW))).rows

    assert parsed.raw_description == ""


def test_a_deposit_is_a_credit() -> None:
    [parsed] = PARSER.parse(
        statement(header(1), row(1, FIRST_ROW, side="deposit", amount="1,500.00"))
    ).rows

    assert (parsed.direction, parsed.amount_paise) == (Direction.CREDIT, 150000)


def test_without_a_serial_the_row_is_numbered_by_position() -> None:
    data = statement(
        header(1),
        row(1, FIRST_ROW, serial=None),
        row(1, FIRST_ROW + 40, serial=None, day="02.08.2026"),
    )

    assert [r.row_number for r in PARSER.parse(data).rows] == [1, 2]


# --- Several rows and pages -------------------------------------------------------


def test_each_row_owns_its_lines_from_just_above_its_dated_line() -> None:
    """The next row's name line (5 pt above it) is close to this row's last
    line (25 pt below this one) - the band boundary keeps them apart."""
    first = (("ASHA", False), ("UPI/ASHA/a@ok/rent", False), ("BANK", False))
    second = (("RAVI", False), ("UPI/RAVI/r@ok/food", False))
    data = statement(
        header(1),
        row(1, FIRST_ROW, serial="1", remarks=first),
        row(1, FIRST_ROW + 40, serial="2", day="02.08.2026", remarks=second),
    )

    assert [r.raw_description for r in PARSER.parse(data).rows] == [
        "UPI/ASHA/a@ok/rent BANK",
        "UPI/RAVI/r@ok/food",
    ]


def test_rows_continue_across_pages_and_the_footer_is_ignored() -> None:
    footer = remark_line("www.icicibank.com Dial your bank", 1, FIRST_ROW + 80)
    data = statement(
        header(1),
        row(1, FIRST_ROW, serial="1", remarks=(("A", False), ("UPI/A/a@x/1", False))),
        footer,
        header(2),
        row(2, FIRST_ROW, serial="2", day="03.08.2026", remarks=(("B", False),)),
    )

    rows = PARSER.parse(data).rows

    assert [(r.row_number, r.raw_description) for r in rows] == [(1, "UPI/A/a@x/1"), (2, "B")]


def test_the_headers_third_line_is_header_not_remarks() -> None:
    """The header's third line lies below every column label. A header word
    of it inside the remarks column was read as remark text above the first
    row - every page refused (review of Step 9)."""
    third_line = [
        word("Date", 1, 61.4, HEADER_TOP + 10),
        word("Amount", 1, 300.0, HEADER_TOP + 10),  # inside the remarks column
        word("(INR)", 1, EDGE, HEADER_TOP + 10),
    ]

    rows = PARSER.parse(statement(header(1), third_line, row(1, FIRST_ROW))).rows

    assert len(rows) == 1


def test_a_legend_page_without_the_header_is_skipped() -> None:
    legend = [word("Legends", 3, 50.0, 100.0), word("Withdrawal", 3, 120.0, 100.0)]
    data = statement(header(1), row(1, FIRST_ROW), legend)

    assert len(PARSER.parse(data).rows) == 1


def test_a_page_with_header_and_no_rows_is_an_empty_statement() -> None:
    """A quiet month is not an error (as for the .xls)."""
    assert PARSER.parse(statement(header(1))).rows == []


# --- Refusals -----------------------------------------------------------------------


def test_text_above_the_first_row_is_an_error_not_dropped() -> None:
    # Below the header's last line, above the first row's band.
    data = statement(header(1), remark_line("ORPHAN", 1, FIRST_ROW - 18), row(1, FIRST_ROW))

    with pytest.raises(ParseError, match="page 1 has remark text above its first transaction"):
        PARSER.parse(data)


def test_a_dated_page_without_the_header_is_an_error() -> None:
    data = statement(header(1), row(1, FIRST_ROW), [word("05.08.2026", 2, 61.4, 300.0)])

    with pytest.raises(ParseError, match="page 2 has dates but no ICICI column header"):
        PARSER.parse(data)


def test_no_header_anywhere_is_not_an_icici_statement() -> None:
    with pytest.raises(ParseError, match="does not look like an ICICI statement"):
        PARSER.parse(statement([word("Hello", 1, 50.0, 50.0)]))


def test_a_dated_line_needs_an_amount_and_a_balance() -> None:
    words = [w for w in row(1, FIRST_ROW, serial="7") if w.text != "120.00"]

    with pytest.raises(ParseError, match="row 7: expected one amount and a balance"):
        PARSER.parse(statement(header(1), words))


def test_an_amount_in_the_balance_column_is_an_error() -> None:
    data = statement(header(1), row(1, FIRST_ROW, serial="3", side="balance"))

    with pytest.raises(ParseError, match="row 3: amount and balance are not in their columns"):
        PARSER.parse(data)


def test_an_impossible_date_names_the_row_not_the_value() -> None:
    data = statement(header(1), row(1, FIRST_ROW, serial="4", day="31.02.2026"))

    with pytest.raises(ParseError, match=r"row 4: transaction date is not a dd\.mm\.yyyy date"):
        PARSER.parse(data)


def test_an_unreadable_amount_names_the_row() -> None:
    data = statement(header(1), row(1, FIRST_ROW, serial="5", amount=",,.00"))

    with pytest.raises(ParseError, match="row 5: amount is not a valid amount"):
        PARSER.parse(data)


def test_a_blank_amount_is_missing() -> None:
    with pytest.raises(ParseError, match="row 9: balance is missing"):
        _paise("", "balance", 9)


# --- detect() ---------------------------------------------------------------------


def test_detect_needs_the_column_header() -> None:
    assert PARSER.detect(statement(header(1))) == 0.95
    assert PARSER.detect(statement([word("Remarks", 1, 260.0, HEADER_TOP)])) == 0.0
    assert PARSER.detect(statement()) == 0.0


# --- Header fields (page 1) -------------------------------------------------------

TITLE = "Statement of Transactions in Savings Account No. 084600001234 in INR for the period "


@pytest.mark.parametrize(
    ("period", "expected"),
    [
        ("August 1, 2026 - August 31, 2026", (date(2026, 8, 1), date(2026, 8, 31))),
        ("Aug 1, 2026 - Sep. 2, 2026", (date(2026, 8, 1), date(2026, 9, 2))),
        ("August 31, 2026 - August 1, 2026", (None, None)),  # backwards: unreadable
        ("Augtober 1, 2026 - August 31, 2026", (None, None)),
    ],
)
def test_the_period_is_read_from_the_title(
    period: str, expected: tuple[date | None, date | None]
) -> None:
    assert _find_period(["header", TITLE + period]) == expected


def test_no_period_line_is_no_period() -> None:
    assert _find_period(["Statement of Transactions"]) == (None, None)


@pytest.mark.parametrize(
    ("title", "last4"),
    [
        (TITLE, "1234"),
        ("Savings Account No. XXXXXXXX1234 in INR", "1234"),
        ("Savings Account No. 0846XXXXXX34 in INR", None),  # tail masked
        ("Savings Account No. NA in INR", None),  # no number printed
        ("Savings statement for August", None),  # no account label
    ],
)
def test_only_the_last_four_digits_leave_the_title(title: str, last4: str | None) -> None:
    assert _find_account_last4([title]) == last4


def test_the_parsed_statement_carries_period_and_account() -> None:
    data = statement(header(1), row(1, FIRST_ROW), title=TITLE + "August 1, 2026 - August 31, 2026")

    parsed = PARSER.parse(data)

    assert (parsed.period_start, parsed.period_end, parsed.account_last4) == (
        date(2026, 8, 1),
        date(2026, 8, 31),
        "1234",
    )


def test_the_account_is_never_read_from_below_the_header() -> None:
    """A narration's "account no ..." must never supply the last four digits:
    only page 1's title block is searched (review of Step 9)."""
    remarks = (("A", False), ("IMPS/account no 987654329999/ref", False))
    words = statement(header(1), row(1, FIRST_ROW, remarks=remarks), title="Savings statement")
    # As the reader builds it: EVERY visual line is in `rows`, narrations too.
    data = ParserInput(
        FileType.PDF,
        rows=[["Savings statement"], ["IMPS/account no 987654329999/ref"]],
        pdf_words=words.pdf_words,
    )

    parsed = PARSER.parse(data)

    assert parsed.account_last4 is None
    assert (parsed.period_start, parsed.period_end) == (None, None)


def test_a_title_on_a_page_without_the_header_is_not_read() -> None:
    """The title block is page 1's, and only when page 1 is a statement page."""
    data = statement(
        title_line(TITLE + "August 1, 2026 - August 31, 2026"),  # page 1, no header
        header(2),
        row(2, FIRST_ROW),
    )

    assert PARSER.parse(data).account_last4 is None
