"""Tests for app.parsers.readers: file detection, CSV and .xls reading.

The `.xls` tests go through a real workbook written by `xlwt`
(tests/fixtures/icici_xls.py and `_xls_bytes` below), not a mock, so the
whole xlrd path is exercised exactly as it will be for a real upload.
"""

import datetime
import io

import pytest
import xlwt

from app.parsers.base import FileType, ParseError
from app.parsers.readers import detect_file_type, read_csv, read_statement, read_xls
from app.utils.money import parse_amount_to_paise

OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _xls_bytes(write) -> bytes:
    """Build a one-sheet .xls in memory; `write(sheet)` fills it in."""
    book = xlwt.Workbook()
    write(book.add_sheet("S"))
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


# --- detect_file_type -----------------------------------------------------


def test_csv_is_detected_from_its_extension() -> None:
    assert detect_file_type("statement.csv", b"Date,Narration\n") is FileType.CSV


def test_txt_is_accepted_as_csv() -> None:
    assert detect_file_type("export.TXT", b"Date;Narration\n") is FileType.CSV


def test_xls_is_detected_from_its_signature() -> None:
    assert detect_file_type("statement.xls", OLE2 + b"rest") is FileType.XLS


def test_the_real_icici_double_extension_name_is_handled() -> None:
    # ICICI really names its download like this.
    name = "OpTransactionHistory28-09-2026.xls-12-34-40.xls"
    assert detect_file_type(name, OLE2 + b"rest") is FileType.XLS


def test_signature_wins_over_a_misleading_name() -> None:
    """A renamed .xls must not be read as CSV (SPEC §7.3)."""
    assert detect_file_type("statement.csv", OLE2 + b"rest") is FileType.XLS


def test_pdf_is_detected() -> None:
    assert detect_file_type("statement.pdf", b"%PDF-1.7\n") is FileType.PDF


def test_xlsx_is_rejected_with_an_actionable_message() -> None:
    with pytest.raises(ParseError) as exc_info:
        detect_file_type("statement.xlsx", b"PK\x03\x04rest")

    assert "xlsx" in str(exc_info.value)


def test_unknown_extension_without_a_signature_is_rejected() -> None:
    with pytest.raises(ParseError, match="unsupported file type"):
        detect_file_type("statement.docx", b"random bytes")


def test_empty_file_is_rejected() -> None:
    with pytest.raises(ParseError, match="empty"):
        detect_file_type("statement.csv", b"")


# --- read_csv -------------------------------------------------------------


def test_read_csv_returns_rows_of_cells() -> None:
    data = read_csv(b"Date,Narration,Amount\n01/08/2026,SHOP,70.00\n")

    assert data.file_type is FileType.CSV
    assert data.rows == [["Date", "Narration", "Amount"], ["01/08/2026", "SHOP", "70.00"]]


def test_read_csv_strips_the_excel_byte_order_mark() -> None:
    """A BOM would otherwise become part of the first column's name."""
    data = read_csv("Date,Narration\n01/08/2026,SHOP\n".encode("utf-8-sig"))

    assert data.rows[0][0] == "Date"


def test_read_csv_sniffs_a_semicolon_delimiter() -> None:
    data = read_csv(b"Date;Narration;Amount\n01/08/2026;SHOP;70.00\n")

    assert data.rows[1] == ["01/08/2026", "SHOP", "70.00"]


def test_read_csv_sniffs_a_tab_delimiter() -> None:
    data = read_csv(b"Date\tNarration\n01/08/2026\tSHOP\n")

    assert data.rows[1] == ["01/08/2026", "SHOP"]


def test_read_csv_keeps_a_comma_inside_a_quoted_field() -> None:
    data = read_csv(b'Date,Narration\n01/08/2026,"SHOP, MUMBAI"\n')

    assert data.rows[1] == ["01/08/2026", "SHOP, MUMBAI"]


def test_read_csv_pads_short_rows() -> None:
    """So a parser can index the balance column without IndexError."""
    data = read_csv(b"Date,Narration,Balance\n01/08/2026,SHOP\n")

    assert data.rows == [["Date", "Narration", "Balance"], ["01/08/2026", "SHOP", ""]]


def test_read_csv_keeps_blank_lines_so_row_numbers_stay_right() -> None:
    data = read_csv(b"Date,Narration\n\n01/08/2026,SHOP\n")

    assert len(data.rows) == 3
    assert data.rows[2][0] == "01/08/2026"


def test_read_csv_falls_back_to_windows_1252() -> None:
    """Excel on Windows writes cp1252 when the file is not UTF-8."""
    data = read_csv("Date,Narration\n01/08/2026,CAFÉ\n".encode("cp1252"))

    assert data.rows[1][1] == "CAFÉ"


def test_read_csv_rejects_bytes_that_are_not_text() -> None:
    # 0x81 and 0x8d are undefined in cp1252 and invalid UTF-8.
    with pytest.raises(ParseError, match="not readable text"):
        read_csv(b"Date,Narration\n\x81\x8d\n")


def test_read_csv_of_a_single_column_file_still_works() -> None:
    """csv.Sniffer raises here; the comma fallback must take over."""
    data = read_csv(b"Narration\nSHOP\n")

    assert data.rows == [["Narration"], ["SHOP"]]


# --- read_xls -------------------------------------------------------------


def test_read_xls_returns_text_cells() -> None:
    content = _xls_bytes(lambda sheet: sheet.write(0, 0, "Transaction Date"))

    data = read_xls(content)

    assert data.file_type is FileType.XLS
    assert data.rows[0][0] == "Transaction Date"


@pytest.mark.parametrize(
    ("value", "expected_text", "expected_paise"),
    [
        (0.29, "0.29", 29),
        (1234.56, "1234.56", 123456),
        (70.0, "70", 7000),
        (0.1 + 0.2, "0.3", 30),
    ],
)
def test_numeric_cells_keep_exact_paise(
    value: float, expected_text: str, expected_paise: int
) -> None:
    """The float-safety rule (ADR 001) at the one place floats enter the app.

    `0.1 + 0.2` is the classic case: it is 0.30000000000000004 as a float,
    and `int(0.30000000000000004 * 100)` is 30 only by luck - `int(0.29 *
    100)` is 28. Going through `Decimal(repr(...))` makes it exact.
    """
    content = _xls_bytes(lambda sheet: sheet.write(0, 0, value))

    text = read_xls(content).rows[0][0]

    assert text == expected_text
    assert parse_amount_to_paise(text) == expected_paise


def test_a_whole_number_cell_has_no_decimal_point() -> None:
    """ICICI's `S No.` column is numeric; the parser checks it with isdigit()."""
    content = _xls_bytes(lambda sheet: sheet.write(0, 0, 175.0))

    assert read_xls(content).rows[0][0] == "175"


def test_a_cell_with_three_decimals_is_passed_on_in_full() -> None:
    """Rounding money inside a reader would hide the problem; the money
    parser is the one that refuses it."""
    content = _xls_bytes(lambda sheet: sheet.write(0, 0, 70.125))

    assert read_xls(content).rows[0][0] == "70.125"


def test_an_excel_date_cell_becomes_an_iso_date() -> None:
    """Excel stores dates as numbers, so the reader must choose a format."""
    content = _xls_bytes(
        lambda sheet: sheet.write(
            0, 0, datetime.date(2026, 8, 1), xlwt.easyxf(num_format_str="DD/MM/YYYY")
        )
    )

    assert read_xls(content).rows[0][0] == "2026-08-01"


def test_a_boolean_cell_becomes_text() -> None:
    content = _xls_bytes(lambda sheet: sheet.write(0, 0, True))

    assert read_xls(content).rows[0][0] == "TRUE"


def test_empty_cells_become_empty_strings() -> None:
    def write(sheet: xlwt.Worksheet) -> None:
        sheet.write(0, 0, "a")
        sheet.write(0, 2, "c")

    assert read_xls(_xls_bytes(write)).rows[0] == ["a", "", "c"]


def test_read_xls_pads_rows_to_one_width() -> None:
    def write(sheet: xlwt.Worksheet) -> None:
        sheet.write(0, 3, "last column of the header")
        sheet.write(1, 0, "short row")

    rows = read_xls(_xls_bytes(write)).rows

    assert [len(row) for row in rows] == [4, 4]


def test_a_file_that_is_not_a_workbook_is_rejected() -> None:
    with pytest.raises(ParseError, match="not a readable .xls workbook"):
        read_xls(OLE2 + b"this is not really a workbook")


# --- read_statement -------------------------------------------------------


def test_read_statement_dispatches_on_the_detected_type() -> None:
    csv_data = read_statement("s.csv", b"Date,Narration\n01/08/2026,SHOP\n")
    xls_data = read_statement("s.xls", _xls_bytes(lambda sheet: sheet.write(0, 0, "hello")))

    assert csv_data.file_type is FileType.CSV
    assert xls_data.file_type is FileType.XLS


def test_read_statement_refuses_a_pdf_for_now() -> None:
    """PDF parsing is Phase 2; the message must say so, not "unsupported"."""
    with pytest.raises(ParseError, match="PDF statements are not supported yet"):
        read_statement("statement.pdf", b"%PDF-1.7\nrest")


def test_a_false_boolean_cell_becomes_text() -> None:
    content = _xls_bytes(lambda sheet: sheet.write(0, 0, False))

    assert read_xls(content).rows[0][0] == "FALSE"


def test_a_date_cell_outside_excels_range_is_rejected() -> None:
    date_style = xlwt.easyxf(num_format_str="DD/MM/YYYY")
    content = _xls_bytes(lambda sheet: sheet.write(0, 0, 3_000_000.0, date_style))

    with pytest.raises(ParseError) as exc_info:
        read_xls(content)

    assert exc_info.value.row_number == 1
    assert exc_info.value.reason == "cell is not a valid Excel date"


def test_a_non_finite_numeric_cell_is_rejected() -> None:
    """A corrupt file could hold an infinity; "inf paise" must never happen."""
    content = _xls_bytes(lambda sheet: sheet.write(0, 0, float("inf")))

    with pytest.raises(ParseError, match="not a finite number"):
        read_xls(content)


def test_read_csv_of_no_content_gives_no_rows() -> None:
    assert read_csv(b"").rows == []
