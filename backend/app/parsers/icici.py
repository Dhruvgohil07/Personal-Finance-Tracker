"""Parse an ICICI Bank "DETAILED STATEMENT" `.xls` download (Step 6, ADR 004).

Written against the real file (`samples/`, gitignored), whose layout was
checked column by column before a line of this was written. What it looks
like, with row numbers as xlrd counts them (0-based):

    row 0    (blank)
    row 1    | DETAILED STATEMENT
    row 3    | Account Number        |   | 0846XXXXXXXX ( INR )  - <HOLDER>
    row 4    | Transaction Date from |   | 01/08/2026 | to | 31/08/2026
    row 11   | Transactions List - <HOLDER> - 0846XXXXXXXX
    row 12   | S No. | Value Date | Transaction Date | Cheque Number |
             | Transaction Remarks | Withdrawal Amount(INR) |
             | Deposit Amount(INR) | Balance(INR)
    row 13.. | 1     | 01/08/2026 | 01/08/2026 |  | UPI/... | 70.00 | 0.00 | 111471.33
    row 194  | Legends Used in Account Statement       <- the table ends here
    row 195+ | 1. INFT - Internal Fund Transfer ...

Facts that shaped the code below, all verified on the real file:

- **Column 0 is always empty**; the table starts in column 1. So nothing
  here assumes a column position - the header labels are searched for, and
  the parser works even if ICICI shifts the whole table sideways.
- **Every cell is text**, including dates and amounts. Dates are
  `dd/mm/yyyy` and amounts are plain `70.00`: no commas, no `Cr`/`Dr`, two
  decimal places. `_parse_date` still accepts ISO as well, because
  `readers.py` renders a genuine Excel date cell that way, and
  `parse_amount_to_paise` handles the other spellings for free.
- **Exactly one of Withdrawal/Deposit is non-zero in every row**; the
  unused side is printed as `0.00`, not left blank. That is what decides
  the direction - no sign guessing.
- **The balance column is internally consistent**: for all 174 steps in the
  sample, `previous balance - withdrawal + deposit == balance`. Phase 2's
  reconciliation (SPEC §6.4) can rely on it.
- **No opening or closing balance is printed anywhere**, so both are
  derived from the rows (see `_statement_balances`).
"""

import re
from datetime import date, datetime

from app.parsers.base import (
    Direction,
    FileType,
    ParsedStatement,
    ParseError,
    ParserInput,
    RawRow,
)
from app.utils.money import MoneyParseError, parse_amount_to_paise

# The table header is found by label, not position. A label is matched on
# its START, so "Withdrawal Amount(INR)" still matches if ICICI changes the
# spacing to "Withdrawal Amount (INR)" or drops the currency suffix.
_DATE_LABEL = "transaction date"
_REMARKS_LABEL = "transaction remarks"
_WITHDRAWAL_LABEL = "withdrawal amount"
_DEPOSIT_LABEL = "deposit amount"
_BALANCE_LABEL = "balance"
_SERIAL_LABEL = "s no"
_VALUE_DATE_LABEL = "value date"

# All of these must be present for the row to be the table header.
_REQUIRED_LABELS = (_DATE_LABEL, _REMARKS_LABEL, _WITHDRAWAL_LABEL, _DEPOSIT_LABEL, _BALANCE_LABEL)

# Labels in the block above the table.
_ACCOUNT_LABEL = "account number"
_PERIOD_LABEL = "transaction date from"

# The line that follows the last transaction.
_LEGEND_MARKER = "legends used"

# How far down we look for the table header. The real file has it at row 12.
_MAX_HEADER_SEARCH_ROWS = 40

# Date spellings accepted in a cell: ICICI's own, then ISO (which is what
# `readers.py` produces from a real Excel date cell - see its docstring).
_DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d")

# Finding the account number inside its cell, which also holds the currency and
# the holder's name. An account identifier is a run of at least six digits and
# masking characters ("084601505606", "XXXXXXXX5606", "0846XXXXXX06"); six keeps
# a stray year like "2026" from matching.
_ACCOUNT_TOKEN = re.compile(r"[0-9Xx*#]{6,}")
# The four digits we may keep (SPEC §7.3 allows no more than four) must be at the
# END of that identifier. Anchoring matters: the first four digits of
# "0846XXXXXX06" are the branch prefix, not the last four, and returning those
# would make Step 7 reject a valid upload as belonging to a different account.
# When the tail is masked, the honest answer is None.
_ACCOUNT_TAIL_DIGITS = re.compile(r"\d{4,}$")

_WHITESPACE = re.compile(r"\s+")


def _label(cell: str) -> str:
    """Normalize a cell for label comparison: lowercase, spaces collapsed."""
    return _WHITESPACE.sub(" ", cell).strip().lower()


class IciciXlsParser:
    """A `StatementParser` for ICICI's `.xls` statement download."""

    bank_code = "ICICI"
    file_types = (FileType.XLS,)

    def detect(self, data: ParserInput) -> float:
        """0.95 when the ICICI table header is present, else 0.0.

        Cheap and exception-free, as the Protocol requires: it only looks at
        the first 40 rows and never parses a date or an amount.

        Why 0.95 and not 1.0? A score is a claim about confidence, and the
        header alone does not *prove* the file is ICICI's - another bank
        could one day print the same five labels. Leaving a margin means a
        future, stricter ICICI parser (one that also checks the account
        header, say) can outrank this one without this one having to change.
        """
        return 0.95 if _find_header_row(data.rows) is not None else 0.0

    def parse(self, data: ParserInput) -> ParsedStatement:
        """Read the whole statement: period, account, rows, balances."""
        header = _find_header_row(data.rows)
        if header is None:
            raise ParseError("this file does not look like an ICICI statement")
        header_index, columns = header

        rows = self._parse_rows(data.rows, header_index, columns)
        period_start, period_end = _find_period(data.rows[:header_index])
        opening, closing = _statement_balances(rows)

        return ParsedStatement(
            rows=rows,
            period_start=period_start,
            period_end=period_end,
            opening_balance_paise=opening,
            closing_balance_paise=closing,
            account_last4=_find_account_last4(data.rows[:header_index]),
        )

    # --- internals --------------------------------------------------------

    def _parse_rows(
        self, rows: list[list[str]], header_index: int, columns: dict[str, int]
    ) -> list[RawRow]:
        """Read the transaction rows that follow the header row.

        Two things make this more than a `for` loop over the rows.

        **Continuation rows.** A long narration does not stay in one cell:
        ICICI spills the rest of it into the next row, which then has *only*
        the Transaction Remarks cell filled - no date, no amounts, no serial
        number. There are six in the sample file. Each one is appended to
        the row above it (`_is_continuation`), so one transaction remains one
        `RawRow`.

        **Where the table ends.** ICICI appends a legend block after the last
        transaction, so the loop stops at a blank row, at the legend marker,
        or at a row with no date that is not a continuation. The rule is
        deliberately asymmetric: an *empty* date cell ends the table, but a
        date cell that is *present and broken* raises `ParseError` with its
        row number. If a bad date silently ended the table instead, one odd
        row in the middle of August would quietly drop the rest of the
        month and every total would be wrong with nothing to show why.
        """
        body = rows[header_index + 1 :]
        parsed: list[RawRow] = []
        index = 0

        while index < len(body):
            row = body[index]
            # 1-based and counted from the top of the file, so the number
            # matches what the user sees when they open it in Excel.
            row_number = header_index + index + 2

            if not any(cell.strip() for cell in row):
                break
            if any(_label(cell).startswith(_LEGEND_MARKER) for cell in row):
                break
            if not _cell(row, columns, _DATE_LABEL).strip():
                # Not a transaction, and not a continuation either (those are
                # consumed below, by the row they belong to), so the table is
                # over - this is a footer line.
                break

            narration = _cell(row, columns, _REMARKS_LABEL)
            index += 1

            # Pull in however many continuation rows follow. They are joined
            # with NOTHING between them, because the bank cuts the narration
            # mid-token: in the sample one row ends "...BANK LTD " (with its
            # own trailing space) and the next begins "D/205412...". Adding a
            # separator here would invent a word boundary that was never in
            # the statement, and since the normalized description feeds the
            # dedupe fingerprint (ADR 006), that would change the identity of
            # the transaction.
            while index < len(body) and _is_continuation(body[index], columns):
                narration += _cell(body[index], columns, _REMARKS_LABEL)
                index += 1

            parsed.append(self._parse_row(row, columns, row_number, narration))

        # The loop above stops at the first row it does not recognise, which is
        # correct for a footer and silently wrong for anything else - a page
        # break or a repeated header in the middle of the table would end the
        # import early and report success with half the month missing. So prove
        # the table really ended: nothing after this point may still look like a
        # transaction.
        _check_table_really_ended(body[index:], columns, header_index + index + 2)

        # An empty table is NOT an error. A dormant or newly opened account has
        # months with no activity, and ICICI still prints the whole header block
        # and the column header row for them. Refusing here would tell the user
        # their file is broken when it is simply quiet, and the upload's file
        # hash is recorded either way, so retrying would not help. "This is not
        # an ICICI statement at all" is a different case, caught by the missing
        # header check in `parse()`; what an empty statement *means* is the
        # import service's decision (Step 7).
        return parsed

    def _parse_row(
        self, row: list[str], columns: dict[str, int], row_number: int, narration: str
    ) -> RawRow:
        """Turn one statement row into a `RawRow`, or raise `ParseError`."""
        # The serial number is an extra guard, not data we keep: if the cell
        # exists and is not a number, this is not a transaction row.
        serial = _cell(row, columns, _SERIAL_LABEL).strip()
        if _SERIAL_LABEL in columns and serial and not serial.isdigit():
            raise ParseError("serial number column is not a number", row_number=row_number)

        amount_paise, direction = self._parse_amount(row, columns, row_number)

        value_date_text = _cell(row, columns, _VALUE_DATE_LABEL).strip()
        return RawRow(
            row_number=row_number,
            txn_date=_parse_date(_cell(row, columns, _DATE_LABEL), "transaction date", row_number),
            amount_paise=amount_paise,
            direction=direction,
            # Stripped only at the two ends: any spacing *inside* belongs to
            # the bank's own text and must survive (see `_parse_rows`).
            raw_description=narration.strip(),
            value_date=(
                _parse_date(value_date_text, "value date", row_number) if value_date_text else None
            ),
            balance_after_paise=_money(_cell(row, columns, _BALANCE_LABEL), "balance", row_number),
        )

    def _parse_amount(
        self, row: list[str], columns: dict[str, int], row_number: int
    ) -> tuple[int, Direction]:
        """Decide the direction from which of the two columns is filled.

        ICICI prints the unused side as `0.00` rather than leaving it empty,
        so zero and blank both mean "not this side" - hence `bool(...)` and
        not `is not None`.
        """
        withdrawal = _money(_cell(row, columns, _WITHDRAWAL_LABEL), "withdrawal", row_number)
        deposit = _money(_cell(row, columns, _DEPOSIT_LABEL), "deposit", row_number)

        if withdrawal and deposit:
            raise ParseError(
                "row has both a withdrawal and a deposit amount", row_number=row_number
            )
        if not withdrawal and not deposit:
            raise ParseError(
                "row has neither a withdrawal nor a deposit amount", row_number=row_number
            )

        # abs(): the column already says which way the money went, so a
        # minus sign or a "Dr" suffix must not flip it a second time
        # (`RawRow` requires a positive amount plus a direction, ADR 001).
        if withdrawal:
            return abs(withdrawal), Direction.DEBIT
        return abs(deposit or 0), Direction.CREDIT


# --- module-level helpers -------------------------------------------------
# These are plain functions, not methods: they depend only on their
# arguments, which makes each one testable on two lines of input.


def _find_header_row(rows: list[list[str]]) -> tuple[int, dict[str, int]] | None:
    """Find the table header and map each known label to its column index.

    Returns None when the header is absent, because `detect()` must answer
    "not mine" without raising. `parse()` turns that None into a
    `ParseError`.

    All five required labels must appear in the SAME row. Looking for them
    together is what makes this reliable: the title row at the top happens
    to contain the word "Statement", but only the real header contains
    "Transaction Date", "Transaction Remarks", "Withdrawal Amount",
    "Deposit Amount" and "Balance" at once.
    """
    known = (
        _SERIAL_LABEL,
        _VALUE_DATE_LABEL,
        _DATE_LABEL,
        _REMARKS_LABEL,
        _WITHDRAWAL_LABEL,
        _DEPOSIT_LABEL,
        _BALANCE_LABEL,
    )

    for index, row in enumerate(rows[:_MAX_HEADER_SEARCH_ROWS]):
        found: dict[str, int] = {}
        for position, cell in enumerate(row):
            text = _label(cell)
            if not text:
                continue
            for label in known:
                # startswith, so "Withdrawal Amount(INR)" matches
                # "withdrawal amount". "Balance(INR)" must not swallow the
                # earlier labels, which is why each label is checked against
                # the start of the cell and not the other way round.
                if text.startswith(label) and label not in found:
                    found[label] = position
                    break
        if all(label in found for label in _REQUIRED_LABELS):
            return index, found
    return None


def _check_table_really_ended(
    remaining: list[list[str]], columns: dict[str, int], first_row_number: int
) -> None:
    """Raise if a transaction row still follows the end of the table.

    `_parse_rows` stops at the first row it cannot read, because that is what a
    footer looks like. The danger is everything else that looks the same: a page
    break, a repeated header, a blank separator row between two statement pages.
    Stopping there would import half the statement and report success, and the
    user would have no way to tell - the numbers would simply be too low.

    So after the loop stops, every remaining row is checked for a readable date
    in the date column. The legend block ICICI appends has nothing in that
    column, so a normal statement passes; anything that still looks like a
    transaction turns the silent truncation into a loud failure with the row
    number to look at.
    """
    for offset, row in enumerate(remaining):
        if _try_parse_date(_cell(row, columns, _DATE_LABEL).strip()) is not None:
            raise ParseError(
                "transaction rows continue after the end of the table",
                row_number=first_row_number + offset,
            )


def _is_continuation(row: list[str], columns: dict[str, int]) -> bool:
    """True when this row only continues the narration of the row above it.

    The test is "narration and nothing else": the remarks cell has text, and
    every other column we know about is empty. Checking the *other* columns
    rather than just "no date" is what keeps a footer line out - the legend
    block below the table also has no date, but it also has no remarks cell,
    while a total row would have an amount.
    """
    if _cell(row, columns, _DATE_LABEL).strip():
        return False
    if not _cell(row, columns, _REMARKS_LABEL).strip():
        return False
    return not any(
        _cell(row, columns, label).strip()
        for label in (
            _SERIAL_LABEL,
            _VALUE_DATE_LABEL,
            _WITHDRAWAL_LABEL,
            _DEPOSIT_LABEL,
            _BALANCE_LABEL,
        )
    )


def _find_period(header_rows: list[list[str]]) -> tuple[date | None, date | None]:
    """Read "Transaction Date from 01/08/2026 to 31/08/2026" above the table.

    Returns `(None, None)` when the label is missing or the dates are not
    readable (ICICI writes "NA" in several of these header fields). The
    statement period is useful metadata, not something to fail an import
    over: Step 7 falls back to the first and last transaction date.
    """
    for row in header_rows:
        if not any(_label(cell).startswith(_PERIOD_LABEL) for cell in row):
            continue
        # The label, the word "to" and the two dates sit in separate cells
        # with blanks between them, so collect whatever parses as a date.
        dates = [parsed for cell in row if (parsed := _try_parse_date(cell.strip())) is not None]
        if len(dates) >= 2 and dates[0] <= dates[-1]:
            return dates[0], dates[-1]
        # Out of order (or only one date found) means we cannot read the period
        # from this header. That must NOT fail the import: `ParsedStatement`
        # rejects a period that ends before it starts, so returning the pair as
        # found would throw away every transaction in a file whose rows are all
        # perfectly readable - over metadata Step 7 can derive from the rows.
        return None, None
    return None, None


def _find_account_last4(header_rows: list[list[str]]) -> str | None:
    """Return the last 4 digits of the account number, or None.

    The cell reads `084601505606 ( INR )  - <HOLDER NAME>`. Only four
    digits leave this function: SPEC §7.3 permits storing no more than
    that, and the holder's name is dropped on the floor rather than
    returned, so it can never reach a log, the database or an LLM prompt.

    Step 7 uses this to check the uploaded file really belongs to the
    account the user picked, instead of silently importing someone else's
    statement into it. That is why a *wrong* answer is worse than no answer:
    it would reject a valid upload with no way for the user to get past it.
    So the four digits are taken only from the END of the account identifier,
    and a masked tail gives None:

        "084601505606 ( INR ) - NAME"  -> "5606"
        "XXXXXXXX5606 ( INR ) - NAME"  -> "5606"
        "0846XXXXXX06 ( INR ) - NAME"  -> None   (last four are not shown)
    """
    for row in header_rows:
        for position, cell in enumerate(row):
            if not _label(cell).startswith(_ACCOUNT_LABEL):
                continue
            # The value sits in a later column, with blank cells between.
            for value in row[position + 1 :]:
                token = _ACCOUNT_TOKEN.search(value)
                if token is None:
                    continue
                # The first account-looking token IS the account, so stop here
                # either way rather than searching on and finding some other
                # number further along the row.
                tail = _ACCOUNT_TAIL_DIGITS.search(token.group())
                return tail.group()[-4:] if tail is not None else None
    return None


def _statement_balances(rows: list[RawRow]) -> tuple[int | None, int | None]:
    """Derive the opening and closing balance from the rows.

    ICICI prints neither, but both follow from the per-row balance column,
    which the sample proves is internally consistent:

        closing = the balance after the LAST row          (a stated fact)
        opening = the balance after the FIRST row, with
                  that row's own effect undone            (one subtraction)

    A debit of ₹70 that left ₹1,114.71 behind means the account held
    ₹1,184.71 before it. Returns `(None, None)` when the statement has no
    balance column, so nothing is invented.

    This is the one place Phase 2's balance reconciliation (SPEC §6.4) can
    check its arithmetic against, which is why it is worth deriving at all.
    """
    if not rows:
        return None, None

    first, last = rows[0], rows[-1]
    if first.balance_after_paise is None or last.balance_after_paise is None:
        return None, None

    # Both derivations assume the rows are in chronological order, which the
    # sample is - but ICICI's net banking can sort the list, and a newest-first
    # export would make "the balance before the first row" the balance before
    # the LATEST transaction. That is not an opening balance, and because
    # neither value would look obviously wrong, Phase 2's reconciliation would
    # end up checking against invented numbers. Refuse instead of inventing.
    if first.txn_date > last.txn_date:
        return None, None

    signed_first = -first.amount_paise if first.direction is Direction.DEBIT else first.amount_paise
    return first.balance_after_paise - signed_first, last.balance_after_paise


def _cell(row: list[str], columns: dict[str, int], label: str) -> str:
    """The value of one labelled column in this row, or "" if absent."""
    index = columns.get(label)
    if index is None or index >= len(row):
        return ""
    return row[index]


def _try_parse_date(text: str) -> date | None:
    """Parse a date in any accepted format, or None if none of them fit."""
    for date_format in _DATE_FORMATS:
        try:
            # Naive on purpose: a statement date is a whole day, stored as
            # DATE (SPEC §5), not a moment in a timezone.
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    return None


def _parse_date(text: str, field: str, row_number: int) -> date:
    """Parse a date cell, raising `ParseError` with the row number."""
    parsed = _try_parse_date(text.strip())
    if parsed is None:
        # The expected format, never the cell's contents (CLAUDE.md rule 3).
        raise ParseError(f"{field} is not a dd/mm/yyyy date", row_number=row_number)
    return parsed


def _money(text: str, field: str, row_number: int) -> int | None:
    """Parse one money cell into signed paise, or None when blank."""
    try:
        return parse_amount_to_paise(text)
    except MoneyParseError as exc:
        raise ParseError(f"{field} is not a valid amount", row_number=row_number) from exc
