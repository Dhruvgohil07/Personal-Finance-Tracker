"""Parse a CSV whose layout the USER describes (SPEC §6.2, Step 6).

Every bank prints its CSV differently, and we cannot write a parser for all
of them. So there is one parser that knows nothing about any bank and
instead takes a `ColumnMapping`: the user says which column holds the date,
which holds the narration, and how the amount is written. That is what
makes "upload a statement from any bank" possible in Phase 1 with only one
real bank parser (ICICI, `app/parsers/icici.py`).

Two ways banks write amounts, and this parser supports both:

    Date,Narration,Withdrawal,Deposit,Balance      <- two columns
    01/08/2026,SHOP,70.00,,111471.33

    Date,Narration,Amount,Balance                  <- one signed column
    01/08/2026,SHOP,-70.00,111471.33

`ColumnMapping` is a frozen dataclass and not a Pydantic model on purpose.
This package is pure (see `app/parsers/__init__.py`): it must stay testable
with nothing but the standard library, and it must not depend on the API
layer. Step 7 adds the Pydantic schema that validates the JSON the user
uploads and then builds one of these, so the dependency points
API -> parsers, never the other way round.
"""

from dataclasses import dataclass
from datetime import datetime

from app.parsers.base import (
    Direction,
    FileType,
    ParsedStatement,
    ParseError,
    ParserInput,
    RawRow,
)
from app.utils.money import MoneyParseError, parse_amount_to_paise

# How many rows from the top we are willing to search for the header. Banks
# put a title or an account summary above the table; nobody puts 50 rows.
_MAX_HEADER_SEARCH_ROWS = 50


@dataclass(frozen=True)
class ColumnMapping:
    """Which column is which, as the user told us.

    Columns are named by their HEADER LABEL, not by position: `"Narration"`,
    not `1`. A label survives the bank adding a column in the middle of the
    file, which a position does not, and it is what the user sees in Excel.
    Matching ignores case and surrounding spaces.

    Exactly one of the two amount styles must be given:

    - `debit_column` **and** `credit_column` - two columns, each holding a
      positive number, the column deciding the direction; or
    - `amount_column` - one column where a negative number (or `Dr`, or
      brackets) means money out.

    `date_format` is a `strptime` pattern, e.g. `"%d/%m/%Y"` for
    `01/08/2026` or `"%d-%b-%Y"` for `01-Aug-2026`. There is no default
    guess: `01/08/2026` is 1 August in India and 8 January in the USA, and
    silently picking one would corrupt every date in the file.

    `__post_init__` raises `ValueError`, not `ParseError`: a broken mapping
    is a bad *request*, not a bad *statement*. Step 7's Pydantic schema
    enforces the same rules at the API edge, so the user gets a 422 with
    field names; this check is the backstop for our own code.
    """

    date_column: str
    description_column: str
    date_format: str
    debit_column: str | None = None
    credit_column: str | None = None
    amount_column: str | None = None
    balance_column: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("date_column", self.date_column),
            ("description_column", self.description_column),
            ("date_format", self.date_format),
        ):
            if not value or not value.strip():
                raise ValueError(f"{name} is required")

        has_debit = self.debit_column is not None
        has_credit = self.credit_column is not None
        has_single = self.amount_column is not None

        # The order of these three checks is the order that gives the most
        # useful message, from the most specific mistake to the vaguest.
        if has_single and (has_debit or has_credit):
            raise ValueError("give either debit_column and credit_column, or amount_column")
        # One half of the pair on its own is the mistake worth catching: it
        # would otherwise read every row as a debit and silently lose all
        # the credits.
        if has_debit != has_credit:
            raise ValueError("debit_column and credit_column must be given together")
        if not has_single and not has_debit:
            raise ValueError("an amount column is required")

        # Each column may be mapped to one role only. Without this check, mapping
        # the same column as both debit and credit is accepted here and then
        # fails on the FIRST row of every upload with "row has both a debit and
        # a credit amount" - an error that blames the user's statement for a
        # mistake in their mapping. Caught here, Step 7's schema can answer 422
        # and name the fields.
        names = [name.strip().lower() for name in self.required_columns]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"each column may be mapped only once: {', '.join(duplicates)}")

    @property
    def required_columns(self) -> tuple[str, ...]:
        """Every column that must exist in the file, in a stable order.

        Used to find the header row (the first row containing all of them)
        and to report a missing column by name.
        """
        columns = [self.date_column, self.description_column]
        for optional in (
            self.debit_column,
            self.credit_column,
            self.amount_column,
            self.balance_column,
        ):
            if optional is not None:
                columns.append(optional)
        return tuple(columns)


class GenericCsvParser:
    """A `StatementParser` driven by a `ColumnMapping` instead of by a bank.

    `detect()` always returns 0.0, which means the registry will never pick
    this parser on its own - and that is deliberate. The registry chooses
    parsers from the file alone, but this one cannot work from the file
    alone: without the user's mapping it does not know which column is the
    date. So "no bank parser recognised this file" and "use the mapping the
    user supplied" stay two separate decisions, and the second one belongs
    to the import service (Step 7, see the note in `registry.py`).
    """

    bank_code = "GENERIC"
    file_types = (FileType.CSV,)

    def __init__(self, mapping: ColumnMapping) -> None:
        self.mapping = mapping

    def detect(self, data: ParserInput) -> float:
        """Never claims a file; see the class docstring."""
        return 0.0

    def parse(self, data: ParserInput) -> ParsedStatement:
        """Read every transaction row the mapping describes.

        Rows are skipped, not rejected, when they are blank or have an
        empty date cell: that is what a separator line, a "Total" footer or
        a trailing note looks like. The exception is a row carrying only a
        description, which continues the previous row's narration. A row that
        *does* have a date but is broken in some other way raises `ParseError`
        with its row number, because silently dropping a real transaction would
        make the imported totals wrong without anyone noticing.

        No statement period or opening/closing balance is reported: a bare
        CSV states none. Step 7 falls back to `ParsedStatement.row_date_range`.
        """
        header_index, columns = self._find_header(data.rows)

        body = data.rows[header_index + 1 :]
        rows: list[RawRow] = []
        index = 0

        while index < len(body):
            row = body[index]
            # Row numbers are 1-based and count from the top of the FILE, so
            # they match what the user sees in Excel.
            row_number = header_index + index + 2
            index += 1

            if not any(cell.strip() for cell in row):
                continue
            if not self._cell(row, columns, self.mapping.date_column).strip():
                continue

            narration = self._cell(row, columns, self.mapping.description_column)

            # A row carrying ONLY a description continues the narration of the
            # row above it, exactly as in `app/parsers/icici.py`: banks wrap a
            # long narration onto a second line. Joined with nothing in between,
            # because the cut can fall mid-token. Dropping these rows instead
            # would truncate the narration, and since the normalized description
            # feeds the dedupe fingerprint (ADR 006), the same transaction would
            # then hash differently depending on which format it was imported
            # from, and its merchant key could be wrong.
            while index < len(body) and self._is_continuation(body[index], columns):
                narration += self._cell(body[index], columns, self.mapping.description_column)
                index += 1

            rows.append(self._parse_row(row, columns, row_number, narration))

        return ParsedStatement(rows=rows)

    # --- internals --------------------------------------------------------

    def _find_header(self, rows: list[list[str]]) -> tuple[int, dict[str, int]]:
        """Find the header row and map each mapped column name to its index.

        The header is the first row that contains *all* the mapped columns.
        Looking for all of them at once is what makes this robust: a bank
        title row might happen to contain the word "Date", but it will not
        contain "Date", "Narration" and "Balance" together.
        """
        wanted = {name.strip().lower() for name in self.mapping.required_columns}

        for index, row in enumerate(rows[:_MAX_HEADER_SEARCH_ROWS]):
            # A header label can repeat (two "Amount" columns); the first
            # one wins, which is why this loop does not overwrite.
            found: dict[str, int] = {}
            for position, cell in enumerate(row):
                label = cell.strip().lower()
                if label in wanted and label not in found:
                    found[label] = position
            if len(found) == len(wanted):
                return index, found

        # Name the columns we were looking for (they are header labels the
        # user chose, not statement data), so the error is actionable.
        raise ParseError(
            "could not find a header row containing the mapped columns: "
            + ", ".join(self.mapping.required_columns)
        )

    def _cell(self, row: list[str], columns: dict[str, int], name: str) -> str:
        """The value of one mapped column in this row.

        Rows are padded to a common width by the reader, so the index is
        always valid; `""` is the answer for a short row anyway.
        """
        index = columns[name.strip().lower()]
        return row[index] if index < len(row) else ""

    def _is_continuation(self, row: list[str], columns: dict[str, int]) -> bool:
        """True when this row only continues the narration of the row above it.

        "Description and nothing else": every other mapped column is empty. A
        "Total" footer has an amount, so it is not mistaken for a continuation.
        """
        mapping = self.mapping
        if self._cell(row, columns, mapping.date_column).strip():
            return False
        if not self._cell(row, columns, mapping.description_column).strip():
            return False
        return not any(
            self._cell(row, columns, name).strip()
            for name in (
                mapping.debit_column,
                mapping.credit_column,
                mapping.amount_column,
                mapping.balance_column,
            )
            if name is not None
        )

    def _parse_row(
        self, row: list[str], columns: dict[str, int], row_number: int, narration: str
    ) -> RawRow:
        """Turn one CSV row into a `RawRow`, or raise `ParseError`."""
        mapping = self.mapping
        txn_date = self._parse_date(self._cell(row, columns, mapping.date_column), row_number)
        amount_paise, direction = self._parse_amount(row, columns, row_number)

        balance_after_paise = None
        if mapping.balance_column is not None:
            # A balance may legitimately be negative (an overdrawn account),
            # so the sign is kept exactly as the statement printed it.
            balance_after_paise = self._money(
                self._cell(row, columns, mapping.balance_column), "balance", row_number
            )

        return RawRow(
            row_number=row_number,
            txn_date=txn_date,
            amount_paise=amount_paise,
            direction=direction,
            # Stripped only at the two ends: spacing *inside* belongs to the
            # bank's own text and matters when continuation rows were joined.
            raw_description=narration.strip(),
            balance_after_paise=balance_after_paise,
        )

    def _parse_date(self, text: str, row_number: int):  # -> date
        """Parse a date cell with the user's `date_format`."""
        try:
            # A statement date is a whole day with no timezone (SPEC §5
            # stores DATE), so a naive parse and `.date()` are correct here.
            return datetime.strptime(text.strip(), self.mapping.date_format).date()
        except ValueError as exc:
            # The message says the format we expected, never the cell's
            # contents (CLAUDE.md rule 3).
            raise ParseError(
                f"date does not match the format {self.mapping.date_format}",
                row_number=row_number,
            ) from exc

    def _parse_amount(
        self, row: list[str], columns: dict[str, int], row_number: int
    ) -> tuple[int, Direction]:
        """Read the amount and its direction, in whichever style was mapped."""
        mapping = self.mapping

        if mapping.amount_column is not None:
            # One signed column: a negative number means money out. Zero and a
            # blank cell are both "no amount here", as in the two-column style
            # below. `abs()` plus a direction, because `RawRow` requires a
            # POSITIVE amount (ADR 001) - and since `_money` already reads
            # "(70.00)" and "70.00 Dr" as negative, those spellings are handled
            # without a case of their own.
            money = self._money(
                self._cell(row, columns, mapping.amount_column), "amount", row_number=row_number
            )
            if money is None or money == 0:
                raise ParseError("row has no amount", row_number=row_number)
            direction = Direction.DEBIT if money < 0 else Direction.CREDIT
            return abs(money), direction

        # Two-column style. `parse_amount_to_paise` returns None for a blank
        # cell, and a bank may print the unused side as "0.00" instead of
        # leaving it empty (ICICI does), so zero counts as "not this side".
        debit = self._money(
            self._cell(row, columns, mapping.debit_column or ""), "debit", row_number
        )
        credit = self._money(
            self._cell(row, columns, mapping.credit_column or ""), "credit", row_number
        )
        has_debit = bool(debit)
        has_credit = bool(credit)

        if has_debit and has_credit:
            raise ParseError("row has both a debit and a credit amount", row_number=row_number)
        if not has_debit and not has_credit:
            raise ParseError("row has no amount", row_number=row_number)

        # abs(): a debit column already means "money out", so a bank that
        # also prints the number as -70.00 or "70.00 Dr" must not flip the
        # direction a second time.
        if has_debit:
            return abs(debit or 0), Direction.DEBIT
        return abs(credit or 0), Direction.CREDIT

    def _money(self, text: str, field: str, row_number: int) -> int | None:
        """Parse one money cell into signed paise, or None when blank."""
        try:
            return parse_amount_to_paise(text)
        except MoneyParseError as exc:
            raise ParseError(f"{field} is not a valid amount", row_number=row_number) from exc
