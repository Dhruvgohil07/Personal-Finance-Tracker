"""The types every statement parser shares (SPEC §6.2).

The ingestion pipeline is a chain of small, pure steps:

    file bytes    -> [reader]      -> ParserInput       (rows of text cells)
    ParserInput   -> [parser]      -> ParsedStatement   (a RawRow per transaction)
    RawRow        -> [normalize]   -> NormalizedRow     (+ description, merchant key)
    NormalizedRow -> [fingerprint] -> sha256 hex string (the dedupe key)

This module defines the data that travels between those steps, plus the
`StatementParser` interface each bank parser implements (Step 6).

Why dataclasses and not Pydantic models? Pydantic earns its keep at the
edges of the app, where untrusted JSON arrives and has to be validated and
documented in /docs. These objects never cross the API boundary: our own
code creates them, they live for the length of one import, and then they
are gone. A frozen dataclass is lighter, needs nothing but the standard
library, and still gives us fixed field names, type hints, a readable
`repr` and immutability.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol

# The largest value a Postgres BIGINT can hold, which is what
# `transactions.amount_paise` and `balance_after_paise` are (SPEC §5).
#
# Python integers have no limit, so `parse_amount_to_paise` happily returns
# a number far beyond this from a corrupt cell - and nothing downstream
# noticed until the INSERT, where Postgres raised a "numeric field
# overflow". That reached the user as a 500 with no row number, instead of
# the 400 every other unreadable row gets. Checking it here means the
# failure is reported like any other bad row.
#
# For scale: this is about ₹92,233,720,368,547,758 - sixteen digits of
# rupees. No real statement row comes close, so a value above it means the
# cell was misread, not that someone is very rich.
MAX_PAISE = 9_223_372_036_854_775_807


def _is_paise(value: object) -> bool:
    """True when `value` is a genuine integer amount in paise.

    `bool` is excluded because it is a subclass of `int` (`True` would be
    read as 1 paisa), and anything non-`int` - above all `float` - is
    rejected outright, which is the rule from ADR 001.
    """
    return isinstance(value, int) and not isinstance(value, bool)


class FileType(StrEnum):
    """The kinds of statement files we can read.

    SPEC §5 lists `csv` and `pdf` for `statement_uploads.file_type`, but
    ICICI hands out legacy Excel `.xls` files (ADR 004), so `xls` is a
    third value. The Step 7 migration uses these same three strings.

    `StrEnum` (Python 3.11+) means `FileType.CSV == "csv"` is True and
    `str(FileType.CSV)` is `"csv"`, so these values compare with and store
    as plain strings while the code still reads as an enum.
    """

    CSV = "csv"
    XLS = "xls"
    PDF = "pdf"


class Direction(StrEnum):
    """Which way the money moved: out of the account, or into it.

    Statements say this in different ways (separate Debit and Credit
    columns, a `Dr`/`Cr` suffix, or one signed column). Parsers turn all of
    them into a POSITIVE `amount_paise` plus this flag, so no query can
    ever accidentally flip a sign (ADR 001).

    Step 7's `transactions` model imports this enum from here. Models may
    import the parser layer; the parser layer must never import models,
    because that would drag SQLAlchemy into this pure package.
    """

    DEBIT = "debit"
    CREDIT = "credit"


class Channel(StrEnum):
    """How the money moved (SPEC §5), read from the narration.

    Detected by `app/parsers/narration.py` (Phase 2 Step 5, ADR 015) and
    stored in `transactions.channel`. It lives here for the same reason as
    `Direction`: the parser layer decides it, and the model imports it from
    here, never the other way round.

    Every value exists in the database CHECK already (migration 0005), so a
    rule for a new channel needs no migration. A narration that matches no
    rule is `OTHER` - "we do not know", not an error.
    """

    UPI = "upi"
    CARD = "card"
    NEFT = "neft"
    IMPS = "imps"
    RTGS = "rtgs"
    ATM = "atm"
    CHEQUE = "cheque"
    ACH_NACH = "ach_nach"
    INTEREST = "interest"
    CHARGES = "charges"
    OTHER = "other"


class ParseError(Exception):
    """A statement we cannot read, with the row number when we know it.

    The message must never contain narration text, amounts or balances
    (CLAUDE.md rule 3): a `ParseError` is logged and shown to the user, so
    it says *what* went wrong and *where*, never *what the data was*.
    `MoneyParseError` in `app/utils/money.py` follows the same rule.

        raise ParseError("amount column is not a number", row_number=12)
        -> str(exc) == "row 12: amount column is not a number"

    `reason` keeps the message without the row prefix, for tests and for
    `statement_uploads.error_message`.
    """

    def __init__(self, reason: str, *, row_number: int | None = None) -> None:
        self.reason = reason
        self.row_number = row_number
        message = reason if row_number is None else f"row {row_number}: {reason}"
        super().__init__(message)


@dataclass(frozen=True)
class ParserInput:
    """A statement file after a reader has turned it into rows of text.

    Readers (Step 6) hide the file format: a CSV is read with the `csv`
    module, an `.xls` with `xlrd`, a PDF with pdfplumber. All of them
    produce the same thing - a list of rows, each a list of string cells -
    so parsers never care which format a file came from, and a test can
    build a `ParserInput` by hand in two lines.

    Every cell is a `str`, including dates and amounts. Turning text into a
    `date` and into paise is the parser's job, because only the parser
    knows the bank's date format and which column holds what.

    `rows` keeps the file's own order and still contains header, footer and
    blank rows: `detect()` needs the header to recognise the bank, so the
    reader must not throw it away.

    `frozen=True` stops the two fields being reassigned. It does not
    deep-freeze the lists inside (Python has no cheap way to do that), so
    by convention parsers read `rows` and never mutate it.
    """

    file_type: FileType
    rows: list[list[str]] = field(default_factory=list)


@dataclass(frozen=True)
class RawRow:
    """One transaction exactly as the statement states it.

    "Raw" means parsed out of the file but not yet interpreted: the
    description is the bank's narration, untouched. Normalization and
    categorization work on a derived copy (`NormalizedRow`), so the
    original text stays available to show the user.

    `row_number` is 1-based and points into the source file. It exists for
    error messages ("row 12 has no date") and is never stored.

    `amount_paise` is always positive and `direction` says which way the
    money went. `__post_init__` enforces that, so an invalid `RawRow`
    cannot be created at all - the same rule the database enforces with
    `CHECK (amount_paise > 0)` (SPEC §5).

    `value_date` (the date the bank actually moved the money) and
    `balance_after_paise` are optional: plenty of statements have neither
    column.

    `__post_init__` checks the *types* of the money and date fields as well
    as their values. That looks paranoid for a dataclass our own parsers
    build, but each of these fields ends up in the dedupe fingerprint
    (ADR 006), so a wrong type does not fail - it silently produces a
    different hash, and the same transaction imported from two file formats
    would no longer match. A loud `ParseError` with a row number is much
    easier to debug than a duplicate transaction found weeks later.
    """

    row_number: int
    txn_date: date
    amount_paise: int
    direction: Direction
    raw_description: str
    value_date: date | None = None
    balance_after_paise: int | None = None

    def __post_init__(self) -> None:
        """Run by the dataclass right after the fields are set.

        Raising `ParseError` here (rather than `ValueError`) means the
        import service sees one exception type for "this file is wrong",
        with the row number attached.
        """
        # `bool` is a subclass of `int` in Python, so `amount_paise=True`
        # would pass a plain `> 0` test. Checking the type also keeps
        # floats out of money (ADR 001).
        if not _is_paise(self.amount_paise):
            raise ParseError("amount must be integer paise", row_number=self.row_number)
        if self.amount_paise <= 0:
            raise ParseError("amount must be greater than zero", row_number=self.row_number)
        # Too big to store (see MAX_PAISE). Caught here rather than at the
        # INSERT, so the user is told which row is wrong instead of getting
        # a 500 - and so no database error carrying this row's values is
        # ever raised (CLAUDE.md rule 3).
        if self.amount_paise > MAX_PAISE:
            raise ParseError("amount is too large to be real", row_number=self.row_number)

        # The balance is money too, and it is the field most likely to
        # arrive as a float: numeric cells in a legacy `.xls` come back
        # from xlrd as Python floats (Step 6).
        if self.balance_after_paise is not None:
            if not _is_paise(self.balance_after_paise):
                raise ParseError("balance must be integer paise", row_number=self.row_number)
            # A balance may legitimately be negative (an overdraft, or a
            # credit card), so both ends are checked.
            if not -MAX_PAISE <= self.balance_after_paise <= MAX_PAISE:
                raise ParseError("balance is too large to be real", row_number=self.row_number)

        # A plain string would pass every comparison in this package,
        # because `Direction` is a StrEnum and `Direction.DEBIT == "debit"`.
        # It only breaks later, where the fingerprint reads `.value`.
        if not isinstance(self.direction, Direction):
            raise ParseError("direction must be a Direction value", row_number=self.row_number)

        # `datetime` is a SUBCLASS of `date`, so a datetime passes
        # `isinstance(x, date)` and then formats as "2026-08-14T00:00:00"
        # instead of "2026-08-14". Statement dates are whole days (SPEC §5
        # stores DATE), and `xlrd.xldate_as_datetime` returns a datetime,
        # so a parser that forgets `.date()` must be caught here.
        for field_name, value in (("date", self.txn_date), ("value date", self.value_date)):
            if value is None:
                continue
            if isinstance(value, datetime) or not isinstance(value, date):
                raise ParseError(f"{field_name} must be a date", row_number=self.row_number)

    @property
    def signed_paise(self) -> int:
        """The row's effect on the balance: negative for a debit, positive for a credit.

        `amount_paise` is always positive and `direction` carries the sign
        (ADR 001); this puts the two back together for arithmetic on
        balances, in one place.
        """
        return -self.amount_paise if self.direction is Direction.DEBIT else self.amount_paise


@dataclass(frozen=True)
class NormalizedRow:
    """A `RawRow` plus the fields derived from its narration (SPEC §6.5).

    It *wraps* the raw row instead of copying its seven fields, so there is
    only ever one source of truth for the amount and the date:

        row.raw.amount_paise        # what the statement said
        row.normalized_description  # derived, goes into the fingerprint
        row.merchant_key            # derived, used by categorization
        row.channel                 # derived: UPI, IMPS, ATM, ...
        row.counterparty_vpa        # derived: the other side's UPI id

    Only `normalized_description` goes into the dedupe fingerprint. The
    other three are read from the narration by `app/parsers/narration.py`
    and can be improved later without changing a single fingerprint (Phase 2
    hard constraint 1, ADR 015).

    `merchant_key` is None when the narration holds nothing merchant-like
    (for example only a reference number, or a cash withdrawal).
    """

    raw: RawRow
    normalized_description: str
    merchant_key: str | None = None
    channel: Channel = Channel.OTHER
    counterparty_vpa: str | None = None


@dataclass(frozen=True)
class ParsedStatement:
    """Everything one parser got out of one file.

    The period comes from the statement's own header when it prints one
    (ICICI's `.xls` does, a bare CSV does not); Step 7 writes it into
    `statement_uploads.period_start/period_end`.

    `opening_balance_paise` / `closing_balance_paise` are set ONLY when the
    statement PRINTS them (a summary block, say). A parser must not derive
    them from the rows: it would have to assume the rows' order, and the
    order is decided later, by `to_chronological` (app/parsers/reconcile.py).
    Balance reconciliation (SPEC §6.4) checks against them when they exist.

    `account_last4` is the last four digits of the account number when the
    statement prints one in its header (ICICI does). Step 7 compares it with
    the account the user picked for the upload, so a statement imported into
    the wrong account is caught instead of quietly adding someone else's
    transactions. Four digits is also the most we are allowed to keep
    (SPEC §7.3), so a parser must truncate before it gets here - never
    return the full number and let a caller shorten it.

    `rows` is listed first because every field after it has a default, and
    in Python a field with a default cannot be followed by one without.
    """

    rows: list[RawRow] = field(default_factory=list)
    period_start: date | None = None
    period_end: date | None = None
    opening_balance_paise: int | None = None
    closing_balance_paise: int | None = None
    account_last4: str | None = None

    def __post_init__(self) -> None:
        if (
            self.period_start is not None
            and self.period_end is not None
            and self.period_start > self.period_end
        ):
            raise ParseError("statement period ends before it starts")

        # A guard against a parser handing over more than it should: this
        # field must be exactly four digits, so a full account number cannot
        # travel any further into the app (SPEC §7.3).
        if self.account_last4 is not None and not (
            len(self.account_last4) == 4 and self.account_last4.isdigit()
        ):
            raise ParseError("account_last4 must be exactly 4 digits")

    @property
    def row_date_range(self) -> tuple[date, date] | None:
        """First and last transaction date, or None when there are no rows.

        Step 7 uses this to fill the upload's period when the parser could
        not read a stated one. It is a property (computed on demand), not a
        stored field, so it can never disagree with `rows`.
        """
        if not self.rows:
            return None
        dates = [row.txn_date for row in self.rows]
        return min(dates), max(dates)


class StatementParser(Protocol):
    """What a bank parser must look like (SPEC §6.2, Strategy pattern).

    A `Protocol` is Python's "structural" interface: a class *is* a
    `StatementParser` simply by having these attributes and methods - it
    does not inherit from anything. The type checker verifies the shape and
    at runtime no base class is involved. That fits parsers well: each one
    is an independent strategy, and a test can pass a five-line fake parser
    without importing this class at all.

    - `bank_code` matches `BankCode` in `app/models/account.py` ("ICICI",
      "GENERIC", ...). It is typed `str` here so this pure layer does not
      import the model.
    - `file_types` lists the formats this parser understands; the registry
      only offers it files of those types.
    - `detect()` returns 0.0-1.0: how sure the parser is that the file is
      its bank's. It must be cheap and must not raise - it only looks at a
      few header cells.
    - `parse()` does the real work and raises `ParseError` on bad data.
    """

    bank_code: str
    file_types: tuple[FileType, ...]

    def detect(self, data: ParserInput) -> float:
        """Confidence between 0.0 and 1.0 that this parser handles the file."""
        ...

    def parse(self, data: ParserInput) -> ParsedStatement:
        """Parse the file, or raise `ParseError` with row context."""
        ...
