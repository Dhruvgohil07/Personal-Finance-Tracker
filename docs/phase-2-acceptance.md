# Phase 2 — acceptance run

The "Done when" list in [plans/phase-2.md](plans/phase-2.md), checked on
2026-11-01 against the live API and the **Docker worker** (not the test
suite's synchronous queue).

- **Part A** uses synthetic statements, so it can cover every case,
  including the ones no real file shows: a statement that does not add up, a
  password-protected PDF, a transfer between two accounts.
- **Part B** uses the two real ICICI statements in `samples/`. Synthetic
  fixtures are drawn from our own reading of the layout, so only the bank's
  own files can show the parsers read it the way the bank meant.

Both were run by scripts that drive the HTTP API exactly as a client would
(register, log in, add accounts, upload, poll `GET /uploads/{id}`, read
`/transactions` and `/insights`). The scripts print counts and PASS/FAIL,
never an amount, a balance or a narration (CLAUDE.md rule 3). They are not
committed: everything they assert is also asserted by `tests/integration/`,
which CI runs (the same decision as in Phase 1).

## Result

| "Done when" (plan) | Result | How |
|---|---|---|
| The real `.xls` **and** PDF import through the worker, balances reconcile | **PASS** | B1, B3 |
| Overlapping statements merge with no duplicates, incl. `.xls` + PDF | **PASS** (synthetic); real: not covered | A2, A3, B5; see [Not covered](#not-covered) |
| A password-protected PDF imports, the password is never persisted | **PASS** | A5, B5, the test suite |
| A broken statement lands in `needs_review` with the bad rows listed | **PASS** | A6 |
| More than half of the real statement auto-categorizes without an LLM | **PASS**: `.xls` 88/175 = **50.3%**, PDF 133/178 = **74.7%** | B6, `test_real_samples.py` |
| Self transfers between two accounts are detected and excluded | **PASS** (synthetic); real: not covered | A8; see [Not covered](#not-covered) |
| `ruff`, `pytest` at 100% coverage, CI green | **PASS** locally; CI on this PR | — |
| Not done on purpose: parsers for other banks | as planned | SPEC §6.2 |

Part A: **21/21** checks. Part B: **13/13** checks. Both were re-run after
the fix this run produced, and again after its code review
([below](#a-finding-the-run-fixed)).

---

## Part A — synthetic statements, live worker

| # | Check | Result |
|---|---|---|
| A1 | `POST /uploads` answers `202` + `pending` + `Retry-After`; the worker imports the `.xls` (5/5/0), balances reconcile | PASS |
| A2 | The same transactions as a PDF: 5 parsed, **0 inserted, 5 duplicate** | PASS |
| A3 | An overlapping statement: 3 parsed, 1 inserted, 2 duplicate | PASS |
| A4 | The same file again: `409 CONFLICT` naming the first upload | PASS |
| A5 | A locked PDF without its password: `409 PASSWORD_REQUIRED`, `needs_password`, no `Retry-After`; a wrong password `400 INCORRECT_PASSWORD`; the right one `202`; imported 9/9 with 0 reconciliation errors; a second password `409` | PASS |
| A6 | A statement with one misprinted balance: `needs_review`, all 3 rows imported, 2 issues at file rows 15 and 16, kind `balance_mismatch` | PASS |
| A7 | A statement for another account number: `failed`, with the "different account" message | PASS |
| A8 | Two accounts, ₹15,000 out of one and into the other a day later: both halves `is_self_transfer`, `heuristic`; the August summary counts only the 2 real expenses and no income | PASS |
| A9 | Auto-categorized on the synthetic UPI statement: 8/9 (informational) | — |
| A10 | Another user: the upload is `404`, the password endpoint is `404`, `/transactions` is empty | PASS |
| A11 | Deleting an upload a later overlapping one relies on: `409`; deleting the latest: `204`, then `404` | PASS |

The worker logged 14 events. A search of the worker's and the API's logs
for every narration, amount, account number, filename and password used
in the run found **none**.

## Part B — the real statements

| File | Period | Rows |
|---|---|---|
| ICICI `.xls` | 2026-08-01 → 2026-08-31 | 175 |
| ICICI PDF | 2026-10-01 → 2026-10-27 | 178 |

Both name the same account (the last 4 digits agree), and the periods do
**not** overlap.

| # | Check | Result |
|---|---|---|
| B0 | Both files name the same account | PASS |
| B1 | `.xls`: `completed` through the worker, 175/175/0, **0 reconciliation issues** | PASS |
| B1 | PDF: `completed` through the worker, 178/178/0, **0 reconciliation issues** | PASS |
| B3 | `GET /transactions` order keeps the balance chain: `.xls` **174/174** links, PDF **177/177** | PASS |
| B4 | The same real file again: `409` | PASS |
| B5 | The real PDF, locked with a test password (AES-128): `409 PASSWORD_REQUIRED`, then unlocked: **0 inserted, 178 duplicates** of the plain PDF's rows | PASS |
| B6 | Auto-categorized without an LLM: `.xls` **88/175 = 50.3%**, PDF **133/178 = 74.7%** | PASS |
| B7 | `GET /insights/summary?month=2026-08` equals the `.xls` rows' own debit and credit totals, 175 transactions | PASS |
| B8 | All acceptance uploads deleted again; no real transaction left in the database | PASS |

**B3 is the check that matters most.** In Phase 1, listing the rows broke
151 of 174 links of the balance chain, because nothing stored the order of
rows within a day. With `statement_seq` (ADR 014), the list order keeps every
link, for both formats. A misread amount, a flipped direction, or a dropped
or duplicated row would each break a link.

**B5 tests the real PDF through the password path.** Locking and unlocking
it changed nothing the fingerprint sees: every row was recognised as already
imported.

How B6 breaks down, by `category_source`:

| | `heuristic` | `merchant` | `keyword` | `none` |
|---|---|---|---|---|
| `.xls` (August) | 67 | 16 | 5 | 87 |
| PDF (October) | 125 | 6 | 2 | 45 |

`heuristic` here is person-to-person UPI (no self transfers in either file:
there is only one real account). **The August `.xls` clears the target by
one row**, so `tests/unit/test_real_samples.py` now re-measures both files'
share from the pure chain on every local run and fails below half. Its `none`
rows are mostly merchant QR payments to shops not in the dictionary. Phase 4's LLM step is what the plan relies on for the rest
(target > 85%).

## A finding the run fixed

The first Part B run categorized the October PDF at **100%** (178/178), and
170 rows were `heuristic`. That was too good to be true. Counting the shapes
of the UPI ids (never the ids themselves) showed that 45 debits, to 3
payees, used **Google Pay for Business** ids: handle `@okbizaxis`, printed by
ICICI cut at 14 characters as `…@okbizax`. The merchant-QR patterns knew
Google Pay for Business only by its `gpay-` prefix, not by its handle. These
payments therefore looked like transfers to people and were filed as
"Transfers to People".

The parser was not at fault: the raw PDF text prints the ids exactly that
way. The fix, decided with Dhruv, gives handles that only businesses get a
list of their own, matched in a way that survives the 14-character cut: a
handle counts if it is printed whole, or, when the VPA is cut, if at least
`okb` of it is left. Google Pay's personal handles (`okaxis`, `okicici`, ...)
share only `ok`, so a handle cut to `ok` stays a person (ADR 016 amendment).

After the fix:
- **PDF:** those 45 rows are `none`, waiting for a correction or Phase 4,
  and the share is 74.7%.
- **August `.xls`:** the first version of this document said it had no such
  handle. That was wrong: the check then only saw the full `@okbiz` prefix.
  Counted properly, it has **1** row cut to `@okb` (now `none`, which takes
  the share from 50.9% to 50.3%). It also has 2 rows cut to `@ok` and 1 to
  `@o`, which cannot be decided, and 64 UPI ids cut before the `@`
  altogether.

Rows already stored keep what the chain decided when they were imported, so
a change like this one is followed by
`python -m app.workers.recategorize_all`, which re-runs every user's rows. It
was run on the development database: 30 users, 30 jobs, no errors.

## Not covered

- **The real `.xls` + PDF overlap.** The two real files share no day, so
  the cross-format dedupe is proven on synthetic files only (A2, and
  `test_the_same_statement_as_xls_and_pdf_is_100_percent_duplicates`).
  `tests/unit/test_real_samples.py` checks it as soon as an `.xls` of the
  PDF's period is in `samples/`.
- **Self transfers between two real accounts.** Only one real account's
  statements exist, so detection is proven on synthetic statements (A8, and
  `tests/integration/test_self_transfers.py`).
- **A real password-protected statement.** None has been seen. B5 locks a
  real PDF with pypdf, so the bank's layout is real but the locking is ours.
- **The October summary.** The PDF covers 1–27 October, not a whole month,
  so `/insights/summary?month=2026-10` cannot be compared with the file's
  own totals. The balance chain (B3) covers its amounts instead.
- **47 PDF rows on Paytm handles** (`pty`, `ptys`) are filed as people.
  Paytm gives those handles to people and shops alike, so this was not
  changed without evidence.
- **Opening and closing balances against netbanking** for the October
  period. The balance chain cannot see a row missing at the very start or
  end of the table. In Phase 1 these were confirmed against netbanking for
  August. To do the same for October, compare the PDF's opening and closing
  balances with netbanking.

---

Anything that does not match on a future run: give **counts, dates and the
mismatching field**, never the statement or a narration from it. Real bank
data stays in `samples/`, which is gitignored (CLAUDE.md rule 8).
