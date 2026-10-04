# ADR 004 — Reading legacy Excel (`.xls`) statements

- Status: accepted
- Date: 2026-10-05 (Phase 1, Step 6)

## Context
SPEC §5 lists `csv` and `pdf` as the statement file types, on the assumption
that a bank either hands out a CSV or a PDF. ICICI — the only bank whose real
statement we have — hands out neither.

"Detailed Statement" downloads from ICICI's net banking are **legacy Excel
97-2003 workbooks**: the first bytes of the real sample are
`D0 CF 11 E0 A1 B1 1A E1`, the OLE2 container signature, and the file is
named `OpTransactionHistory28-09-2026.xls-12-34-40.xls`. Python's `csv`
module cannot read a binary workbook, and neither can `openpyxl`, which
supports only the modern zip-and-XML `.xlsx` format.

The users of this app upload exactly what their bank gives them. Expecting
them to open the file in Excel and re-save it as CSV first would make the
core feature — "upload your statement" — something most people would get
wrong, and the people most likely to get it wrong are the ones the app is
for.

What the file actually looks like was checked column by column before any
parser was written (CLAUDE.md §17: ask about statement formats, never
guess). One sheet, `OpTransactionHistory`, 222 rows; column 0 empty; a
header block with the account number and the statement period; the column
labels in row 12; 175 transactions from row 13; and a 27-line legend block
at the bottom. **Every cell is text**, including dates (`dd/mm/yyyy`) and
amounts (`70.00`, no commas, no `Cr`/`Dr`).

## Decision
Support `.xls` as a first-class statement format.

1. **New dependency `xlrd` (>= 2.0.2)** for reading. From version 2.0 it
   reads `.xls` only, which is exactly the scope we want: no surprise
   `.xlsx` or `.xlsm` support, no C extension, pure Python.
2. **`FileType.XLS`** joins `csv` and `pdf` in `app/parsers/base.py`, and
   `statement_uploads.file_type` (Step 7) accepts the same three values.
3. **One reader module, `app/parsers/readers.py`**, turns bytes of either
   format into the same structure — a list of rows of string cells — so
   every parser is format-blind. A file's **signature decides what it is**,
   not its name: a renamed `.xls` is still read as Excel (SPEC §7.3), and a
   `.xlsx` is refused with a message that says what to do instead.
4. **`xlwt` as a dev-only dependency** to generate synthetic `.xls` fixtures
   in tests. Nothing in the app ever writes a workbook.

### Supporting decisions
- **Numeric cells are converted with `Decimal(f"{value:.15g}")`.** Excel
  stores numbers as IEEE doubles, so xlrd hands back a `float` — the one
  place a float can enter this codebase (ADR 001). 15 significant digits is
  the precision Excel itself keeps and displays, so a cell the bank computed
  with a formula reads as `0.3` rather than `0.30000000000000004`. The
  alternatives both fail: `Decimal(0.29)` expands the exact binary value,
  and `Decimal(repr(0.29 + ...))` keeps 17 digits, which
  `parse_amount_to_paise` then rejects — failing a whole statement over a
  rounding artefact. Beyond the 15-digit cut nothing is rounded: a cell that
  really holds three decimals is passed on in full and refused by the money
  parser, because quietly rounding money inside a reader would hide the
  problem instead of reporting it. A unit test asserts exact paise for
  `0.29`, `1234.56` and `0.1 + 0.2`.
- **A whole number renders without a decimal point** (`"175"`, not
  `"175.0"`). These cells are not always money: ICICI's `S No.` and cheque
  columns are numbers, and the parser checks them with `str.isdigit()`.
- **Excel date cells render as ISO 8601.** A date cell is a number, so there
  is no original text to hand back and the reader must choose a format; ISO
  is the only unambiguous one (is `01/08/2026` January or August?). Parsers
  therefore accept their bank's own format *and* ISO.
- **Rows are padded to one width.** A CSV line can have fewer commas and
  xlrd ends a row at its last non-empty cell, so without padding every
  parser would have to guard every cell access against `IndexError`.
- **Only the first sheet is read.** ICICI's download has exactly one;
  guessing between several is a decision for whichever bank needs it.
- **A split narration is joined with nothing in between.** ICICI spills a
  long narration into the following row, which then has only the Transaction
  Remarks cell filled (six such rows in the sample). The cut is *mid-token*:
  one row ends `...ACME BANK LTD ` and the next begins `D/205412345678/...`.
  Inserting a separator would invent a word boundary the statement never
  had, and since the normalized description feeds the dedupe fingerprint
  (ADR 006), that would change the transaction's identity — and its merchant
  key. Direct concatenation reproduces the bank's own string, and the joined
  narrations come out at 98–100 characters, matching the longest
  single-cell narrations in the same file.
- **The opening and closing balance are derived from the rows.** ICICI
  prints neither, but the per-row balance column is internally consistent
  (`previous − withdrawal + deposit == balance` holds for all 174 steps in
  the sample), so the closing balance is the last row's balance and the
  opening balance is the first row's balance with that row's own effect
  undone. Both are `None` when a statement has no balance column, so nothing
  is invented. This gives Phase 2's reconciliation (SPEC §6.4) something to
  check against.
- **The parser returns only the last 4 digits of the account number.** The
  header cell holds the full 12-digit number, the currency and the account
  holder's name together. `ParsedStatement.account_last4` is validated to be
  exactly four digits, so a full number cannot travel further into the app
  (SPEC §7.3), and the holder's name is dropped rather than returned. Step 7
  compares those four digits with the account the user picked, so a
  statement uploaded into the wrong account is caught instead of silently
  adding transactions to it.
- **Test fixtures are generated, not committed.** `tests/fixtures/icici_xls.py`
  writes a real workbook (OLE2 signature and all) into `tmp_path` from
  plain-Python row tuples. A committed binary cannot be reviewed in a diff,
  and every variation a test needs — a credit row, a broken date, a
  continuation row, a missing period — would be another blob. No real
  statement is ever committed (CLAUDE.md rule 8).

## Alternatives rejected
- **Ask users to convert to CSV first.** Pushes the hardest step onto the
  user, and a silent mis-conversion (Excel reformatting dates, or writing
  cp1252) produces a wrong import rather than an error.
- **`pandas.read_excel`.** Brings in pandas and numpy for what is a few
  hundred rows of text, and its DataFrame coerces types on the way in —
  exactly the float conversion this project must control itself. ADR 001
  would be enforced on top of a library that had already broken it.
- **`openpyxl`.** Reads `.xlsx` only; it cannot open this file at all.
- **Convert server-side with LibreOffice.** A heavyweight external process
  per upload, and the uploaded file would have to be written to disk, which
  SPEC §7.3 is specifically trying to avoid.
- **Guess the format from the file extension alone.** The real ICICI name
  ends in `.xls`, but names are user-editable and a renamed file would then
  be fed to the wrong reader. The signature costs one comparison.
- **`xlutils`/`xlwt` in production.** Writing workbooks is a test need only.
  `xlwt` stays in the dev dependency group.

## Consequences
- The app can read the real ICICI statement. End to end on the sample file:
  175 transactions, 148 debits and 27 credits, period 2026-08-01 to
  2026-08-31 read from the header, 175 distinct fingerprints, and
  `opening − debits + credits == closing` exactly in integer paise.
- `xlrd` is unmaintained in practice (2.0.2, 2024) but small, pure Python
  and limited to a frozen file format. `xlwt` (1.3.0, 2017) is unmaintained
  too and is dev-only; it still runs on Python 3.12 and writes a genuine
  OLE2 workbook, which is what makes the fixtures honest.
- `FileType` now has three values, so Step 7's migration and every place
  that switches on the file type must handle `xls`.
- PDF is detected properly and then refused with "PDF statements are not
  supported yet", so the upload row can still record its file type. PDF
  parsing is Phase 2.
- A bank that ships `.xlsx` will need another reader (and probably
  `openpyxl`). The split between `readers.py` and the parsers means that is
  one new function plus a `FileType` value, not a change to any parser.
