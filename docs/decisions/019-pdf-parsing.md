# ADR 019 — PDF parsing (ICICI)

- Status: accepted
- Date: 2026-10-27 (Phase 2, Step 9)

## Context
ICICI hands out statements as `.xls` (ADR 004) and as PDF. The plan's key requirement
is that the same transactions imported in both formats are the **same rows**: the second
import must be 100% duplicates. The fingerprint (ADR 006) includes the normalized
narration, so the PDF must reproduce the `.xls` narration exactly.

The real PDF (14 pages, 178 rows, all UPI) was profiled before any design. Only
structure was inspected (positions, counts, lengths and masked shapes), never content:

- **No grid.** pdfplumber's table extraction finds only the header row on each page,
  so the rows must be rebuilt from word positions.
- **Seven columns at fixed x positions on every page:** S No., Transaction Date
  (`DD.MM.YYYY`), Cheque Number, Transaction Remarks, Withdrawal, Deposit, Balance.
  There is no value date, and no opening or closing balance. The header repeats on
  every page, and the last page ends with a legend.
- **A row's dated line** holds the S No., the date, one amount and the balance. The
  amount's column gives the direction. Withdrawal, deposit and balance chain correctly
  on 150 of 150 checked steps between consecutive rows on a page.
- **Remarks are vertically centred:** they start one line (5 pt) above the dated line
  and run 3–5 lines down, 10 pt apart. The previous row's last line sits 15 pt above,
  never 5.
- **The first remark line repeats the counterparty's name**, then comes
  `UPI/name/vpa/remark/bank/RRN/txn-id`. The `.xls` narration has no such line.
- **Wrapping:** at spaces when possible, and mid-token only when a token is wider than
  the line (the 35-character txn id). The space at a break is not printed. All 177
  mid-token splits end 12.0–17.1 pt short of the column edge. All 128 space breaks
  after a one-word line end 32.5 pt or more short of it.
- **Fields are cut like the `.xls`:** name ≤10, VPA ≤14, remark and bank ≤10
  characters.
- **Page 1's title** carries the full account number and
  `for the period <Month D, YYYY> - <Month D, YYYY>`.

## Decision
1. **The reader passes word positions.** `ParserInput` gains `pdf_words: list[PdfWord]`
   (text, page, x0, x1, top, bottom). For a PDF, `rows` holds each visual line as a
   one-cell row, for `detect()` and header fields. `read_pdf` (pdfplumber) lives in
   `readers.py`. The parser stays pure and is unit-tested on hand-built words (decided
   with Dhruv).
2. **`IciciPdfParser`** locates the columns per page from the header labels.
   - A row owns its remark lines from 10 pt above its dated line down to 10 pt above
     the next row's.
   - Below a page's last row, the first gap larger than 15 pt is the footer.
   - Remark text above a page's first row, or dates on a page without the header, are
     a `ParseError`, never dropped silently.
3. **Joining wrapped lines.** Join with nothing when the upper line is ONE word ending
   within 25 pt of the Withdrawal column (a token split mid-way). Otherwise join with a
   space (a space break). On the real file this rebuilds 177 of 178 narrations with
   exactly the `.xls` field widths, txn id 35 characters every time. The remaining
   narration has 5 fields in the bank's own text.
4. **The name line is dropped when it equals the UPI name field below it**, and kept
   otherwise (decided with Dhruv). This makes the narration identical to the `.xls`.
5. **Rows of an unseen kind** (NEFT, IMPS, ATM in a PDF) are parsed with the same rules
   (decided with Dhruv). Their narration has never been compared with the `.xls`.
6. **Only the last 4 digits** of the title's account number are kept (SPEC §7.3).
   `account_last4` is now shared with the `.xls` parser.
7. **PDFs are accepted at upload** (no more 400). A damaged or password-protected PDF
   fails in the worker with a message. Passwords arrive in Step 10.

## Alternatives rejected
- **pdfplumber's table extraction:** it finds no data rows on this file.
- **Joining always with nothing, or always with a space:** the first loses the spaces in
  bank and remark fields, the second inserts one into every txn id. Either way the
  fingerprints stop matching the `.xls`.
- **A nearest-row rule for remark lines:** it ties exactly halfway between rows (25 pt).
- **Keeping the name line:** a PDF and an `.xls` of the same period would then never
  deduplicate.
- **The parser reading the PDF itself:** it would do I/O and need real files for every
  test, unlike every other parser.

## Consequences
- **The cross-format guarantee is proven on synthetic files.** On the real files it
  needs an `.xls` and a PDF of the same period. The two in `samples/` do not overlap
  (checked with a counts-only comparison script).
- **The join rule depends on measured geometry** (25 pt). A layout change by the bank
  (font, column width) shows up as narrations that no longer match the `.xls`. The
  parser does not detect this by itself.
- **Theoretical ambiguity:** a one-word line that genuinely ends a word within 25 pt of
  the edge would be joined without its space. None of 128 real space breaks came
  closer than 32.5 pt.
- **New dev dependency `fpdf2`** for the synthetic PDF builder (`tests/fixtures/
  icici_pdf.py`), which draws the measured layout.

## Amendment — code review of PR 24 (8 findings, all addressed)
Two decisions were made with Dhruv (marked *decided*).

1. **The account's last 4 and the period come only from page 1's title block** (the
   words above the column header). Before, every text line of the PDF was searched, so
   a narration such as "account no 9876543210" could have supplied the digits had the
   title's wording changed.
2. **Any failure while reading a PDF becomes one `ParseError`.** pdfplumber does not
   wrap every failure: its public `extract_words()` was seen raising a bare `IndexError`
   on a damaged PDF. The finding named `MalformedPDFException`, which pdfplumber in fact
   wraps; the broad catch covers both. Only third-party parsing of the uploaded bytes
   runs inside it.
3. **Remarks carried over to the next page join the previous row** (*decided*). They
   must run on from just under the header without a gap. On every real page the first
   remark line starts 6.9 pt below the header, and the footer about 700 pt below. A
   page holding only a continuation still drops its footer. On the first statement
   page, or for text that does not run on from the header, it stays an error.
4. **The name line matches even when the name field ends in a space.** The bank may
   cut the name right after a space ("RAM KUMAR /vpa").
5. **`read_statement_as` dispatches each file type by name** and refuses anything else.
   Before, every non-CSV, non-XLS type went to `read_pdf`.
6. **The header block's extent comes from all its words**, including its third line
   ("Date", "Amount (INR)"). Before, it came only from the five column labels.
7. **A local real-file cross-check**, `tests/unit/test_real_samples.py` (*decided*). It
   checks that every `.xls` and PDF in `samples/` covering a common day give the same
   transactions on those days. It asserts counts only and skips where no such pair
   exists, CI included. This answers the finding that the synthetic cross-format test
   is circular, because its PDFs are drawn by our own fixture.
8. **Words are split by page once**, and each word's row is found by binary search.

The real PDF parses to identical rows, period and last 4 after every one of these
changes (checked with a digest of the parsed output, counts and hashes only).
