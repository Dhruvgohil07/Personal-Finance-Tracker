"""Build a SYNTHETIC ICICI PDF statement for tests (CLAUDE.md rule 8).

The PDF twin of tests/fixtures/icici_xls.py: it takes the SAME row tuples
(serial, value date, txn date, cheque, remarks, withdrawal, deposit,
balance), so a test can render one set of transactions as both `.xls` and
PDF and prove the two imports agree.

It draws the layout measured on the real ICICI PDF (ADR 019), with made-up
data:

- the title on page 1 (account number, period), the column header on every
  page, a footer at the bottom of every page and a legend at the end;
- each row's dated line, with its remarks starting one line ABOVE it and
  running down 10 pt apart;
- for a UPI narration, the extra first line holding just the name field;
- the remarks wrapped the way the bank does it: at a space when possible,
  mid-token only when a token is wider than a whole line, 14 pt inside the
  column's edge.

`fpdf2` is the writer (dev dependency only - the app never writes a PDF):
pure Python, and it places text at exact coordinates, which is all a
statement layout needs. With `password` set, the file is encrypted the way
a bank's password-protected statement is.

`encrypt_pdf` re-encrypts finished PDF bytes with pypdf instead, for the
tests that need a particular encryption method (RC4, AES-128, AES-256) or
an owner password only. Two different writers on purpose: the app unlocks
with pypdf, so a test file encrypted by fpdf2 shows that the unlocking is
not just pypdf agreeing with itself.
"""

import io
from pathlib import Path
from typing import Final

from fpdf import FPDF
from pypdf import PdfReader, PdfWriter

from tests.fixtures.icici_xls import DEFAULT_ROWS

# Column positions (points), as measured on the real file.
SERIAL_X: Final = 30.1
DATE_X: Final = 61.4
REMARKS_LEFT: Final = 185.58  # where the "Number" label ends
REMARKS_X: Final = 192.0
EDGE: Final = 398.98  # the Withdrawal column starts here
WRAP_EDGE: Final = EDGE - 14.0  # the bank wraps this far inside the column
DEPOSIT_X: Final = 473.79
BALANCE_X: Final = 532.14
WITHDRAWAL_AMOUNT_X: Final = 431.9
DEPOSIT_AMOUNT_X: Final = 490.0
BALANCE_AMOUNT_X: Final = 535.5

FONT_SIZE: Final = 7
LINE: Final = 10.0  # remark lines are this far apart
TITLE_Y: Final = 120.0
HEADER_Y: Final = 210.0  # baseline of the header's second line
FIRST_ROW_Y: Final = 250.0
LAST_ROW_Y: Final = 740.0  # no row line goes below this
LEGEND_Y: Final = 765.0
FOOTER_Y: Final = 800.0

LEGEND_LINES: Final = (
    "Legends Used in Account Statement",
    "INFT - Internal Fund Transfer (Within ICICI Bank)",
)


def build_icici_pdf(
    path: Path,
    rows: tuple[tuple[str, ...], ...] = DEFAULT_ROWS,
    *,
    account_number: str = "084600001234",
    period: str = "August 1, 2026 - August 31, 2026",
    password: str | None = None,
) -> Path:
    """Write a synthetic ICICI PDF statement to `path` and return that path."""
    pdf = FPDF(unit="pt", format="A4")
    pdf.set_auto_page_break(False)
    pdf.set_font("Helvetica", size=FONT_SIZE)
    if password is not None:
        pdf.set_encryption(owner_password="owner-" + password, user_password=password)

    pdf.add_page()
    pdf.text(
        30.0,
        TITLE_Y,
        f"Statement of Transactions in Savings Account No. {account_number} in INR "
        f"for the period {period}",
    )
    _header(pdf)
    y = FIRST_ROW_Y
    for row in _merge_continuations(rows):
        lines = _remark_lines(pdf, row[4])
        # The dated line sits one line below the row's first remark line.
        height = LINE * max(len(lines), 2)
        if y + height - LINE > LAST_ROW_Y:
            _footer(pdf)
            pdf.add_page()
            _header(pdf)
            y = FIRST_ROW_Y
        _row(pdf, y, row, lines)
        y += height
    # The legend between the last row and the footer, more than a line's
    # gap below any row line, as on the real last page.
    for index, line in enumerate(LEGEND_LINES):
        pdf.text(30.0, LEGEND_Y + LINE * index, line)
    _footer(pdf)

    path.write_bytes(bytes(pdf.output()))
    return path


def encrypt_pdf(
    content: bytes,
    *,
    user_password: str,
    owner_password: str = "owner-secret",
    algorithm: str = "AES-128",
) -> bytes:
    """`content` (an unlocked PDF) encrypted with pypdf.

    `user_password` opens the file; an EMPTY one makes a PDF anyone can
    open, restricted only by its owner password. `algorithm` is one of
    pypdf's names: "RC4-40", "RC4-128", "AES-128", "AES-256-R5", "AES-256".
    """
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(content)))
    writer.encrypt(user_password=user_password, owner_password=owner_password, algorithm=algorithm)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def is_encrypted(content: bytes) -> bool:
    """Is this PDF password-protected (by any password, even an empty one)?"""
    return PdfReader(io.BytesIO(content)).is_encrypted


def _header(pdf: FPDF) -> None:
    """The two-line column header, labels placed where the parser looks."""
    top, bottom = HEADER_Y - LINE, HEADER_Y
    pdf.text(SERIAL_X, top, "S")
    pdf.text(SERIAL_X, bottom, "No.")
    pdf.text(DATE_X, top, "Transaction")
    pdf.text(DATE_X, bottom, "Date")
    pdf.text(REMARKS_LEFT - pdf.get_string_width("Cheque"), top, "Cheque")
    pdf.text(REMARKS_LEFT - pdf.get_string_width("Number"), bottom, "Number")
    pdf.text(260.0, top, "Transaction")
    pdf.text(260.0, bottom, "Remarks")
    pdf.text(EDGE, top, "Withdrawal")
    pdf.text(EDGE, bottom, "Amount (INR)")
    pdf.text(DEPOSIT_X, top, "Deposit")
    pdf.text(DEPOSIT_X, bottom, "Amount (INR)")
    pdf.text(BALANCE_X, top, "Balance")
    pdf.text(BALANCE_X, bottom, "(INR)")


def _footer(pdf: FPDF) -> None:
    """Page footer text, some of it inside the remarks column on purpose."""
    pdf.text(REMARKS_X, FOOTER_Y, "www.icicibank.com Dial your bank 1800-1080")
    pdf.text(30.0, FOOTER_Y + LINE, "This is a system generated statement.")


def _row(pdf: FPDF, y: float, row: tuple[str, ...], lines: list[str]) -> None:
    serial, _value_date, txn_date, _cheque, _remarks, withdrawal, deposit, balance = row
    pdf.text(SERIAL_X, y, serial)
    pdf.text(DATE_X, y, txn_date.replace("/", "."))
    if _is_zero(withdrawal):
        pdf.text(DEPOSIT_AMOUNT_X, y, deposit)
    else:
        pdf.text(WITHDRAWAL_AMOUNT_X, y, withdrawal)
    pdf.text(BALANCE_AMOUNT_X, y, balance)
    # The first remark line half a line above the dated line, then one per LINE.
    for index, line in enumerate(lines):
        pdf.text(REMARKS_X, y - LINE / 2 + LINE * index, line)


def _is_zero(amount: str) -> bool:
    return amount.strip() in {"", "0", "0.00"}


def _merge_continuations(rows: tuple[tuple[str, ...], ...]) -> list[tuple[str, ...]]:
    """Fold the `.xls` continuation rows (only remarks filled) into their row.

    Joined with NOTHING, exactly as the `.xls` parser joins them, so both
    builders describe the same narration.
    """
    merged: list[tuple[str, ...]] = []
    for row in rows:
        if row[0] == "" and row[2] == "" and merged:
            previous = merged[-1]
            merged[-1] = (*previous[:4], previous[4] + row[4], *previous[5:])
        else:
            merged.append(tuple(row))
    return merged


def _remark_lines(pdf: FPDF, narration: str) -> list[str]:
    """The narration as the bank prints it: a name line (UPI only), then wrapped."""
    lines = []
    fields = narration.split("/")
    if len(fields) > 1 and fields[0] == "UPI":
        lines.append(fields[1])
    lines.extend(_wrap(pdf, narration))
    return lines


def _wrap(pdf: FPDF, text: str) -> list[str]:
    """Greedy wrap at WRAP_EDGE: at spaces, splitting only a too-wide token."""
    width = WRAP_EDGE - REMARKS_X
    lines: list[str] = []
    current = ""
    for token in text.split(" "):
        candidate = f"{current} {token}" if current else token
        if pdf.get_string_width(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        # A token wider than a whole line is cut, character by character.
        while pdf.get_string_width(token) > width:
            cut = len(token)
            while pdf.get_string_width(token[:cut]) > width:
                cut -= 1
            lines.append(token[:cut])
            token = token[cut:]
        current = token
    if current:
        lines.append(current)
    return lines
