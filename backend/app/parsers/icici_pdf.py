"""ICICI Bank's PDF statement (Phase 2 Step 9, ADR 019).

The `.xls` download is a grid of cells (app/parsers/icici.py). The PDF is
the same statement drawn on pages: text at positions, with no grid at all
- pdfplumber finds a "table" holding only the column headers. So this
parser rebuilds the rows from word POSITIONS (`ParserInput.pdf_words`).

Everything below was measured on a real 14-page ICICI PDF (178 rows) and
confirmed with Dhruv. Only shapes, positions and counts were inspected,
never the content:

**The table.** Seven columns, the same x positions on every page:
`S No.`, `Transaction Date` (DD.MM.YYYY), `Cheque Number`,
`Transaction Remarks`, `Withdrawal Amount (INR)`, `Deposit Amount (INR)`,
`Balance (INR)`. The header is repeated on every page. There is no value
date, and no opening or closing balance anywhere.

**A row** is anchored by its DATED line: S No., date, one amount and the
balance, all on one line. The amount's column says the direction. The
remarks are vertically centred on that line, so they start ONE line above
it and run down for 3-5 lines (10 pt apart):

         -5   MOON CAFE                       <- the counterparty's name
     ^0   1   01.08.2026          120.00   99880.00
         +5   UPI/MOON CAFE/q1234@ybl/NO
        +15   REMARK/YES BANK
        +25   L/512345678901/ICI4f2a9c1e7b3d5f6a8c0
        +35   e2b4d6f8a1c3e

So a row owns everything from just above its own first line down to just
above the next row's (`_ROW_STARTS_ABOVE`).

**The name line.** The first remark line repeats the UPI name field. The
`.xls` narration does not have it, and the same transaction must produce
the SAME narration from both files (its fingerprint, ADR 006) - so the
line is dropped when it equals the name field of the UPI text below it,
and kept otherwise (decided with Dhruv).

**Joining wrapped lines** - the one non-obvious rule (`_join`). The bank
wraps the remark at the column's edge: at a space when it can, and
mid-token only when a token is wider than a whole line (the 35-character
transaction id). The space at a break is not printed, so each break must
be classified:

- the upper line is ONE word filling the column to its edge -> the token
  was split -> join with nothing;
- anything else -> the line broke at a space -> join with a space.

Checked on the real file: it rebuilds every UPI narration with exactly the
field widths of the `.xls` (name <=10, VPA <=14, remark and bank <=10, txn
id 35). "Filling the column" is measured, see `_FULL_LINE_TOLERANCE`.

All 178 real rows are UPI. A row of another kind is parsed with the same
rules (decided with Dhruv, ADR 019); its narration has never been compared
with the `.xls` and may differ.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from app.parsers.base import (
    Direction,
    FileType,
    ParsedStatement,
    ParseError,
    ParserInput,
    PdfWord,
    RawRow,
)
from app.parsers.icici import account_last4
from app.utils.money import MoneyParseError, parse_amount_to_paise

_DATE = re.compile(r"\d{2}\.\d{2}\.\d{4}")
_AMOUNT = re.compile(r"-?[\d,]+\.\d{2}")
_SERIAL = re.compile(r"\d+")

# The header labels that locate the columns. "Number" closes the Cheque
# Number column (the remarks start after it); the three amount labels open
# their columns.
_HEADER_LABELS = frozenset({"Number", "Remarks", "Withdrawal", "Deposit", "Balance"})
# Every word the three-line header prints. Its extent - where the page's
# body starts, and (on page 1) where the title block ends - is measured
# from ALL of them, not only the labels above: the third line ("Date",
# "Amount (INR)", "(INR)") lies below every label, and was read as body
# text (review of Step 9).
_HEADER_WORDS = _HEADER_LABELS | {
    "S",
    "No.",
    "Transaction",
    "Date",
    "Cheque",
    "Amount",
    "(INR)",
}
# A header word belongs to the header block when it is this close to the
# "Remarks" label (the header spans three lines).
_HEADER_HEIGHT = 25.0

# Words this close vertically are on the same line.
_SAME_LINE = 2.0
# How far above its dated line a row's first remark line may start. The
# measured first line sits 5 pt above; the previous row's last line sits
# 15 pt above, so the boundary goes halfway between.
_ROW_STARTS_ABOVE = 10.0
# Below the last row of a page, remark lines run on 10 pt apart; a larger
# jump is the page footer, never part of a row (measured: the footer starts
# 23 pt or more after a row's last line).
_MAX_LINE_GAP = 15.0
# How close to the column's edge (the Withdrawal column's start) a one-word
# line must reach to count as FULL, i.e. split mid-token (module
# docstring). Measured on the real file: all 177 mid-token splits end
# 12.0-17.1 pt from that edge (the bank wraps a little inside the column),
# all 128 space breaks after a one-word line end 32.5 pt or more from it.
# 25 pt sits in the middle of that gap.
_FULL_LINE_TOLERANCE = 25.0

# "... for the period August 1, 2026 - August 31, 2026" on page 1.
_PERIOD = re.compile(
    r"period\s+([A-Za-z]+\.?\s+\d{1,2},\s*\d{4})\s*-\s*([A-Za-z]+\.?\s+\d{1,2},\s*\d{4})"
)
_PERIOD_FORMATS = ("%B %d, %Y", "%b %d, %Y")
_ACCOUNT_LABEL = "account no"


@dataclass(frozen=True)
class _Columns:
    """Where one page's columns are, from its header labels."""

    remarks_left: float  # right edge of the Cheque Number column
    remarks_right: float  # the Withdrawal column starts here
    deposit_left: float
    balance_left: float
    header_top: float  # the header's first line; above it, the title block
    header_bottom: float  # the header's last line; below it, the rows


@dataclass(frozen=True)
class _Line:
    """One visual line of a row's remarks."""

    words: tuple[PdfWord, ...]

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words)

    @property
    def top(self) -> float:
        return self.words[0].top

    @property
    def x1(self) -> float:
        return max(word.x1 for word in self.words)


class IciciPdfParser:
    """A `StatementParser` for ICICI's PDF statement."""

    bank_code = "ICICI"
    file_types = (FileType.PDF,)

    def detect(self, data: ParserInput) -> float:
        """0.95 when a page carries ICICI's column header, else 0.0.

        0.95 for the same reason as the `.xls` parser: the header alone is
        strong evidence, not proof.
        """
        pages = {word.page for word in data.pdf_words}
        found = any(_columns(_words_on(data.pdf_words, page)) is not None for page in pages)
        return 0.95 if found else 0.0

    def parse(self, data: ParserInput) -> ParsedStatement:
        """Read every page's rows, then the period and account from page 1's title."""
        pages = sorted({word.page for word in data.pdf_words})
        parts: list[_RowParts] = []
        title: list[str] = []
        header_seen = False
        for page in pages:
            words = _words_on(data.pdf_words, page)
            columns = _columns(words)
            if columns is None:
                # A page without the header (the legend page, say) may not
                # carry a transaction: a dated line here would be a row we
                # cannot read, and must not be skipped silently.
                if any(_is_date_word(word) for word in words):
                    raise ParseError(f"page {page} has dates but no ICICI column header")
                continue
            if page == pages[0]:
                title = _title_lines(words, columns)
            header_seen = True
            continuation, page_parts = _page_parts(words, columns, page, len(parts) + 1)
            if continuation:
                # Remarks the bank carried over from the previous page's last
                # row (decided with Dhruv, review of Step 9). On the first
                # statement page there is no row to carry them over from.
                if not parts:
                    raise ParseError(f"page {page} has remark text above its first transaction")
                parts[-1].remarks.extend(continuation)
            parts.extend(page_parts)
        if not header_seen:
            raise ParseError("this file does not look like an ICICI statement")

        # Only now, with every page read: a row's remarks may still have
        # grown with the next page's continuation.
        rows = [_parse_row(part) for part in parts]
        period_start, period_end = _find_period(title)
        return ParsedStatement(
            rows=rows,
            period_start=period_start,
            period_end=period_end,
            account_last4=_find_account_last4(title),
        )


# --- Columns and rows -------------------------------------------------------


def _words_on(words: Sequence[PdfWord], page: int) -> list[PdfWord]:
    return [word for word in words if word.page == page]


def _is_date_word(word: PdfWord) -> bool:
    return _DATE.fullmatch(word.text) is not None


def _columns(words: Sequence[PdfWord]) -> _Columns | None:
    """The page's column positions, or None if its header is not there.

    Labels are taken from the header block only (near "Remarks"): the
    legend on the last page also says "Withdrawal" and "Deposit".
    """
    remarks = next((word for word in words if word.text == "Remarks"), None)
    if remarks is None:
        return None
    block = [
        word
        for word in words
        if word.text in _HEADER_WORDS and abs(word.top - remarks.top) <= _HEADER_HEIGHT
    ]
    labels: dict[str, PdfWord] = {}
    for word in block:
        if word.text in _HEADER_LABELS:
            labels.setdefault(word.text, word)
    if set(labels) != _HEADER_LABELS:
        return None
    return _Columns(
        remarks_left=labels["Number"].x1,
        remarks_right=labels["Withdrawal"].x0,
        deposit_left=labels["Deposit"].x0,
        balance_left=labels["Balance"].x0,
        header_top=min(word.top for word in block),
        header_bottom=max(word.bottom for word in block),
    )


@dataclass
class _RowParts:
    """One transaction's pieces, gathered before it becomes a `RawRow`.

    Not frozen: `remarks` may still grow when the next page carries the
    rest of this row's remarks over (see `parse`).
    """

    line: list[PdfWord]  # the dated line
    anchor: PdfWord  # its date
    remarks: list[_Line]
    columns: _Columns
    fallback_number: int


def _page_parts(
    words: Sequence[PdfWord], columns: _Columns, page: int, first_row_number: int
) -> tuple[list[_Line], list[_RowParts]]:
    """One page's transactions, plus remark lines carried over from the previous page."""
    body = [word for word in words if word.top > columns.header_bottom]
    # A dated line is a date in the columns LEFT of the remarks.
    anchors = sorted(
        (word for word in body if _is_date_word(word) and word.x1 <= columns.remarks_left),
        key=lambda word: word.top,
    )
    continuation, remark_lines = _remark_lines(body, columns, anchors, page)
    parts = [
        _RowParts(
            line=_same_line(body, anchor),
            anchor=anchor,
            remarks=remark_lines[index],
            columns=columns,
            fallback_number=first_row_number + index,
        )
        for index, anchor in enumerate(anchors)
    ]
    return continuation, parts


def _remark_lines(
    body: Sequence[PdfWord], columns: _Columns, anchors: Sequence[PdfWord], page: int
) -> tuple[list[_Line], list[list[_Line]]]:
    """Each row's remark lines: from just above its dated line to the next row's.

    Also returns the lines ABOVE the page's first row: remarks the bank
    carried over from the previous page's last row. They run on from just
    under the header, 10 pt apart (the first remark line of every real page
    starts 6.9 pt below it), so they end at the first larger gap - which on
    a page without rows is the footer.

    Text in the remarks column that belongs to nothing - neither a row, a
    carried-over line nor the footer below the last row - is an error
    rather than dropped.
    """
    in_column = [
        word
        for word in body
        if word.x0 >= columns.remarks_left and word.x1 <= columns.remarks_right
    ]
    above: list[PdfWord] = []
    owned: list[list[PdfWord]] = [[] for _ in anchors]
    for word in in_column:
        owner = None
        for index, anchor in enumerate(anchors):
            if anchor.top - _ROW_STARTS_ABOVE <= word.top:
                owner = index
        if owner is None:
            above.append(word)
        else:
            owned[owner].append(word)

    above_lines = _group_lines(above)
    continuation = _until_gap(above_lines, columns.header_bottom)
    if anchors and len(continuation) != len(above_lines):
        raise ParseError(f"page {page} has remark text above its first transaction")

    rows = [_group_lines(words) for words in owned]
    if anchors:
        # Below the page's LAST row, cut at the first gap: that is the footer.
        rows[-1] = _until_gap(rows[-1], anchors[-1].top)
    return continuation, rows


def _until_gap(lines: Sequence[_Line], start: float) -> list[_Line]:
    """The lines that run on from `start` without a gap wider than a line.

    Lines at or above `start` are always kept (a row's first remark line
    sits above its dated line).
    """
    kept: list[_Line] = []
    previous = start
    for line in lines:
        if line.top > start and line.top - previous > _MAX_LINE_GAP:
            break
        kept.append(line)
        previous = max(previous, line.top)
    return kept


def _group_lines(words: Sequence[PdfWord]) -> list[_Line]:
    """Words -> lines, top to bottom, each line's words left to right."""
    lines: list[list[PdfWord]] = []
    for word in sorted(words, key=lambda word: (word.top, word.x0)):
        if lines and abs(lines[-1][0].top - word.top) <= _SAME_LINE:
            lines[-1].append(word)
        else:
            lines.append([word])
    return [_Line(tuple(sorted(line, key=lambda word: word.x0))) for line in lines]


def _same_line(words: Sequence[PdfWord], anchor: PdfWord) -> list[PdfWord]:
    return sorted(
        (word for word in words if abs(word.top - anchor.top) <= _SAME_LINE),
        key=lambda word: word.x0,
    )


def _parse_row(parts: _RowParts) -> RawRow:
    """One transaction from its dated line and its remark lines."""
    line, anchor, remarks, columns = parts.line, parts.anchor, parts.remarks, parts.columns
    fallback_number = parts.fallback_number
    serial = next(
        (word.text for word in line if word.x1 <= anchor.x0 and _SERIAL.fullmatch(word.text)),
        None,
    )
    # The S No. the user sees in the PDF, so an error points at the right row.
    row_number = int(serial) if serial is not None else fallback_number

    amounts = [
        word
        for word in line
        if word.x0 >= columns.remarks_right and _AMOUNT.fullmatch(word.text) is not None
    ]
    if len(amounts) != 2:
        raise ParseError("expected one amount and a balance", row_number=row_number)
    amount, balance = amounts
    if _centre(balance) < columns.balance_left or _centre(amount) >= columns.balance_left:
        raise ParseError("amount and balance are not in their columns", row_number=row_number)
    direction = Direction.DEBIT if _centre(amount) < columns.deposit_left else Direction.CREDIT

    return RawRow(
        row_number=row_number,
        txn_date=_parse_date(anchor.text, row_number),
        # abs(): the column already gives the direction (ADR 001).
        amount_paise=abs(_paise(amount.text, "amount", row_number)),
        direction=direction,
        raw_description=_narration(remarks, columns),
        balance_after_paise=_paise(balance.text, "balance", row_number),
    )


def _centre(word: PdfWord) -> float:
    return (word.x0 + word.x1) / 2


def _narration(lines: Sequence[_Line], columns: _Columns) -> str:
    """The remark lines as the ONE narration the `.xls` prints."""
    if len(lines) >= 2:
        rest = _join(lines[1:], columns)
        fields = rest.split("/")
        # The name line repeats the UPI name field (module docstring). The
        # field may end in a space - the bank cut the name after one, and
        # the word after it starts with "/" ("UPI/RAM KUMAR /q1@ybl") -
        # while a line never does, so compare without it (review of Step 9).
        if len(fields) > 1 and fields[0] == "UPI" and fields[1].strip() == lines[0].text:
            return rest
    return _join(lines, columns)


def _join(lines: Sequence[_Line], columns: _Columns) -> str:
    """Join wrapped lines, restoring the spaces the line breaks hid."""
    if not lines:
        return ""
    text = lines[0].text
    for upper, lower in zip(lines, lines[1:], strict=False):
        split_token = (
            len(upper.words) == 1 and upper.x1 >= columns.remarks_right - _FULL_LINE_TOLERANCE
        )
        text += ("" if split_token else " ") + lower.text
    return text


def _parse_date(text: str, row_number: int) -> date:
    try:
        return datetime.strptime(text, "%d.%m.%Y").date()
    except ValueError:
        # The expected format, never the text (CLAUDE.md rule 3).
        raise ParseError(
            "transaction date is not a dd.mm.yyyy date", row_number=row_number
        ) from None


def _paise(text: str, field: str, row_number: int) -> int:
    try:
        paise = parse_amount_to_paise(text)
    except MoneyParseError:
        raise ParseError(f"{field} is not a valid amount", row_number=row_number) from None
    if paise is None:
        # Blank: the row's regex never passes one, but this function stays
        # correct on its own rather than leaning on its caller.
        raise ParseError(f"{field} is missing", row_number=row_number)
    return paise


# --- Header fields (page 1's title block) -----------------------------------


def _title_lines(words: Sequence[PdfWord], columns: _Columns) -> list[str]:
    """Page 1's text ABOVE the column header: the title block, as lines.

    The period and the account number are read from here only - never from
    the rows or the legend below. A narration such as "account no
    9876543210" further down must never be able to supply the account's
    last four digits; the `.xls` parser likewise searches only the block
    above its table (review of Step 9).
    """
    return [line.text for line in _group_lines([w for w in words if w.top < columns.header_top])]


def _find_period(lines: Sequence[str]) -> tuple[date | None, date | None]:
    """The statement period from "... for the period <from> - <to>", or (None, None).

    Metadata, never a reason to fail an import (as for the `.xls`).
    """
    for line in lines:
        match = _PERIOD.search(line)
        if match is None:
            continue
        start, end = (_try_month_date(text) for text in match.groups())
        if start is not None and end is not None and start <= end:
            return start, end
        return None, None
    return None, None


def _try_month_date(text: str) -> date | None:
    cleaned = re.sub(r"\s+", " ", text.replace(".", "")).replace(" ,", ",")
    for date_format in _PERIOD_FORMATS:
        try:
            return datetime.strptime(cleaned, date_format).date()
        except ValueError:
            continue
    return None


def _find_account_last4(lines: Sequence[str]) -> str | None:
    """The last 4 digits of the account number in the title line, or None.

    The title prints the FULL number ("... Savings Account No. 084601505606
    in INR ..."); only four digits ever leave this function (SPEC §7.3).
    """
    for line in lines:
        position = line.lower().find(_ACCOUNT_LABEL)
        if position != -1:
            return account_last4(line[position + len(_ACCOUNT_LABEL) :])
    return None
