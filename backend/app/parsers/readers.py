"""Turn uploaded bytes into rows of text cells (SPEC §6.1, Step 6).

This is the only module in the parser package that knows about *file
formats*. Everything after it works on the same simple structure - a list
of rows, each a list of string cells - so a parser never cares whether the
statement arrived as a CSV or as a legacy Excel file:

    bytes -> [detect_file_type] -> FileType
    bytes -> [read_statement]   -> ParserInput(file_type, rows)

Three rules shape this module:

1. **The file's own bytes decide what it is, not its name.** A user can
   rename `statement.xls` to `statement.csv`; the signature check below
   still reads it as Excel, and a file whose name says `.csv` but whose
   content is something we cannot read is rejected instead of being fed to
   the CSV parser as mojibake (SPEC §7.3: validate every input).
2. **Every cell comes out as a `str`.** Only the bank's own parser knows
   which column is a date in which format and which column is money, so
   converting text into `date` and into paise is the parser's job, not the
   reader's (see `app/parsers/base.py`).
3. **Numbers never pass through `float` arithmetic.** Numeric cells in an
   `.xls` arrive as IEEE doubles, so `_number_to_text` converts them with
   `Decimal` before they become text (ADR 001). The real ICICI statement
   happens to store every cell as text, so this path exists for other
   banks' files - and it has its own test, because a silent rounding error
   in money is exactly the bug this project must not have.
"""

import csv
import io
from decimal import Decimal
from pathlib import PurePosixPath

import xlrd

from app.parsers.base import FileType, ParseError, ParserInput

# The first bytes of a file ("magic number"). These are fixed by the file
# formats themselves, so they identify a file far better than its name.
#
# D0 CF 11 E0 A1 B1 1A E1 is the OLE2 container that holds a legacy Excel
# 97-2003 `.xls` workbook - the format ICICI hands out (ADR 004).
_OLE2_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# PK\x03\x04 is a ZIP archive, and a modern `.xlsx` is a ZIP of XML files.
# We detect it only so the error message can say something useful.
_ZIP_SIGNATURE = b"PK\x03\x04"
_PDF_SIGNATURE = b"%PDF-"

# A CSV is plain text and has no signature, so for CSV the file name is all
# we have to go on.
_CSV_EXTENSIONS = frozenset({".csv", ".txt"})

# Encodings tried in order when decoding a CSV. `utf-8-sig` is UTF-8 and
# also swallows the byte-order mark Excel writes at the start of a file
# ("﻿"), which would otherwise become part of the first column's
# header name and break a column mapping. cp1252 (Windows-1252) is what
# Excel on a Windows machine writes when the file is not UTF-8; it accepts
# almost any byte sequence, so it doubles as a last resort.
_CSV_ENCODINGS = ("utf-8-sig", "cp1252")

# Delimiters csv.Sniffer is allowed to choose between. Restricting the list
# matters: given a free choice, Sniffer can decide that some character
# inside a narration is the delimiter and shred every row.
_CSV_DELIMITERS = ",;\t|"


def detect_file_type(filename: str, content: bytes) -> FileType:
    """Work out what kind of file this is, trusting the bytes first.

        detect_file_type("statement.xls", b"\\xd0\\xcf\\x11\\xe0...") -> FileType.XLS
        detect_file_type("anything.csv", b"Date,Narration\\n...")     -> FileType.CSV

    Order of decisions:

    1. a recognised signature wins, whatever the file is called;
    2. a ZIP signature gets its own message, because "save it as .xls or
       CSV" is actionable and "unsupported file" is not;
    3. with no signature the file must be text, and then only a `.csv`
       (or `.txt`) name is accepted.

    Raises `ParseError` rather than returning a guess, because every later
    step assumes the file type is known.
    """
    if not content:
        raise ParseError("file is empty")

    if content.startswith(_PDF_SIGNATURE):
        return FileType.PDF
    if content.startswith(_OLE2_SIGNATURE):
        return FileType.XLS
    if content.startswith(_ZIP_SIGNATURE):
        raise ParseError("modern Excel .xlsx files are not supported yet; upload .xls or CSV")

    # PurePosixPath handles the name the browser sent us; `suffix` is the
    # part from the last dot, so "OpTransactionHistory28-09.xls-12-34.xls"
    # gives ".xls" - which is how ICICI really names its downloads.
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix in _CSV_EXTENSIONS:
        return FileType.CSV

    raise ParseError("unsupported file type; upload a CSV or an .xls statement")


def read_statement(filename: str, content: bytes) -> ParserInput:
    """Detect the file type and read the file into rows.

    This is what the import service (Step 7) calls. A PDF is detected
    properly - so the upload row can record `file_type = "pdf"` - and then
    refused, because PDF parsing is Phase 2 (Phase 1 plan).
    """
    file_type = detect_file_type(filename, content)

    if file_type is FileType.CSV:
        return read_csv(content)
    if file_type is FileType.XLS:
        return read_xls(content)
    raise ParseError("PDF statements are not supported yet")


def read_csv(content: bytes) -> ParserInput:
    """Read CSV bytes into rows of text cells.

    Decoding is tried as UTF-8 (with or without a BOM) and then as
    Windows-1252; the delimiter is sniffed from the start of the file among
    `,` `;` tab `|`.

    Blank lines are kept. They carry no transaction, but dropping rows here
    would shift every `row_number` and make the parser's error messages
    point at the wrong line of the user's file.
    """
    text = _decode_csv(content)

    # csv.Sniffer reads a sample and guesses the dialect. It raises when the
    # sample has no consistent delimiter - a single-column file, for
    # instance - and a comma is then exactly the right assumption.
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(
            text[:8192], delimiters=_CSV_DELIMITERS
        )
    except csv.Error:
        dialect = csv.excel

    # newline="" is what the csv module documents: the reader must see the
    # raw line endings itself, because a quoted field may contain one.
    reader = csv.reader(io.StringIO(text, newline=""), dialect)
    return ParserInput(file_type=FileType.CSV, rows=_padded(list(reader)))


def read_xls(content: bytes) -> ParserInput:
    """Read a legacy Excel `.xls` workbook into rows of text cells (ADR 004).

    Only the FIRST sheet is read. ICICI's download has exactly one
    (`OpTransactionHistory`), and guessing which of several sheets holds
    the transactions is a decision for whichever bank actually needs it.

    `xlrd` is given the bytes directly (`file_contents=`), so nothing is
    written to disk: uploaded statements only ever live in memory and in
    the encrypted blob store (SPEC §7.3).
    """
    try:
        book = xlrd.open_workbook(file_contents=content)
    except Exception as exc:  # broad: xlrd raises several unrelated types
        # xlrd signals a damaged or unexpected workbook with XLRDError, but
        # also with ValueError or IndexError from deep inside its BIFF
        # reader, so the only safe net is a broad one. The message says
        # nothing about the contents (CLAUDE.md rule 3).
        raise ParseError("file is not a readable .xls workbook") from exc

    if book.nsheets == 0:
        raise ParseError("workbook has no sheets")

    sheet = book.sheet_by_index(0)
    rows = [
        [
            _cell_to_text(sheet.cell(row_index, col_index), book.datemode, row_index + 1)
            # xlrd keeps each row only as long as its last non-empty cell,
            # so the width is per row here and `_padded` squares it off.
            for col_index in range(len(sheet.row_values(row_index)))
        ]
        for row_index in range(sheet.nrows)
    ]
    return ParserInput(file_type=FileType.XLS, rows=_padded(rows))


def _decode_csv(content: bytes) -> str:
    """Decode CSV bytes, trying UTF-8 (BOM-safe) and then Windows-1252."""
    for encoding in _CSV_ENCODINGS:
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ParseError("file is not readable text; expected a UTF-8 or Windows-1252 CSV")


def _cell_to_text(cell: xlrd.sheet.Cell, datemode: int, row_number: int) -> str:
    """Convert one Excel cell into the text a parser will work with.

    xlrd gives every cell a type code, and each needs its own treatment:

    | xlrd cell type      | becomes                                     |
    |---------------------|---------------------------------------------|
    | EMPTY / BLANK       | `""`                                        |
    | TEXT                | the string itself                           |
    | NUMBER (a float)    | exact decimal text, `70.0` -> `"70"`        |
    | DATE (a float)      | ISO date, `"2026-08-01"`                     |
    | BOOLEAN             | `"TRUE"` / `"FALSE"`                        |
    | ERROR (`#N/A`, ...) | `""` - an error cell has no usable value    |

    Note what happens to a DATE cell: Excel stores dates as numbers, so
    there is no "original text" to hand back and the reader must choose a
    format. It chooses ISO 8601, the one unambiguous option (is
    `01/08/2026` January or August?). A parser therefore has to accept its
    bank's own date format *and* ISO; `app/parsers/icici.py` does exactly
    that.
    """
    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return ""
    if cell.ctype == xlrd.XL_CELL_TEXT:
        return str(cell.value)
    if cell.ctype == xlrd.XL_CELL_NUMBER:
        return _number_to_text(cell.value, row_number)
    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            # Excel counts days from 1899-12-31 or, on old Macs, from
            # 1904-01-01; `datemode` comes from the workbook and says which.
            return xlrd.xldate_as_datetime(cell.value, datemode).date().isoformat()
        except Exception as exc:  # broad: XLDateError, but also ValueError
            raise ParseError("cell is not a valid Excel date", row_number=row_number) from exc
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return "TRUE" if cell.value else "FALSE"
    return ""


def _number_to_text(value: float, row_number: int) -> str:
    """Render a numeric Excel cell as exact decimal text (ADR 001).

    `f"{value:.15g}"` is the key line: it prints the double with **15
    significant digits**, which is the precision Excel itself keeps and
    shows. Anything beyond those 15 digits is an artefact of binary floating
    point, not something the bank wrote.

    Two wrong alternatives, for contrast:

        >>> Decimal(0.29)        # the double's exact binary value
        Decimal('0.289999999999999980015985556747182272374629974365234375')
        >>> repr(0.1 + 0.2)      # shortest round-tripping string
        '0.30000000000000004'
        >>> f"{0.1 + 0.2:.15g}"  # what Excel would display
        '0.3'

    `Decimal(0.29)` would need rounding later anyway, and `repr()` would
    turn a cell Excel computed with a formula into a 17-digit number that
    `parse_amount_to_paise` then refuses - rejecting a whole statement over
    a rounding artefact.

    A whole number comes back without a decimal point (`"175"`, not
    `"175.0"`), because these cells are not always money: ICICI's `S No.`
    and cheque-number columns are numbers too, and the parser checks them
    with `str.isdigit()`.

    Beyond the 15-digit cut, nothing is rounded. A cell that really holds
    three decimal places is passed on in full and `parse_amount_to_paise`
    refuses it, which is the honest outcome - quietly rounding money inside
    a reader would hide the problem instead of reporting it.
    """
    number = Decimal(f"{value:.15g}")
    if not number.is_finite():
        # Excel cannot normally store an infinity or NaN in a cell, but a
        # corrupt file could. Better a clear error than "inf" paise.
        raise ParseError("numeric cell is not a finite number", row_number=row_number)
    if number == number.to_integral_value():
        return str(int(number))
    # format(..., "f") prints the plain decimal form, never 1E+20.
    return format(number, "f")


def _padded(rows: list[list[str]]) -> list[list[str]]:
    """Give every row the same number of cells, padding short ones with "".

    Both readers produce ragged rows: a CSV line can simply have fewer
    commas, and xlrd stops a row at its last non-empty cell. A parser that
    found the balance in column 8 of the header row would then have to
    guard every single `row[8]` against IndexError. One pad here removes
    that whole class of bug from every parser.
    """
    if not rows:
        return []
    width = max(len(row) for row in rows)
    return [[*row, *([""] * (width - len(row)))] for row in rows]
