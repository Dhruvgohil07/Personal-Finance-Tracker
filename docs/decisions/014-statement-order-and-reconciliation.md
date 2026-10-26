# ADR 014 — Statement order and balance reconciliation

- Status: accepted (amends ADR 009's cursor)
- Date: 2026-10-26 (Phase 2, Step 4)

## Context
A bank statement prints dates but no times, and several transactions often share a
day. On the real ICICI statement, **every one of its 30 days** has several. Until
now nothing stored the order the statement printed them in. `GET /transactions`
sorted a day's rows by their `id`, a random UUID, so one day came back shuffled.
The balance column then jumped up and down. The Phase 1 acceptance run found 151 of
174 balance links "broken" in the API's order, against 174/174 in the file's own
order (progress.md, Known issues).

SPEC §6.4 also asks for **balance reconciliation**. Each row's balance must be the
previous balance plus or minus its amount, and opening + credits − debits must
equal closing. A statement that fails is kept but marked `needs_review`, with the
rows that look wrong. This catches parser bugs (a misread amount, a skipped row) on
the way in.

## Decision

**1. `to_chronological` (pure, app/parsers/reconcile.py).** Before anything is
stored, a statement's rows are put oldest-first. Rows are only kept or **reversed,
never sorted**. Sorting by date would scramble the order within a day, which is the
very information only the file has.
- dates never go down: kept
- dates never go up, and do change (a newest-first export): reversed
- every row on one day: the balance chain decides the direction
- dates go both ways: kept, and reconciliation will report it

**2. `transactions.statement_seq`** = the row's position in that chronological list
(1, 2, 3, …), with `UNIQUE (upload_id, statement_seq)` and `CHECK (> 0)`
(migration 0010). The list order and the keyset index become
`(txn_date DESC, statement_seq DESC, id DESC)`. The cursor carries all three parts.
A two-part cursor from before is refused (422), so a client starts again from the
first page rather than landing somewhere unexpected.

**3. `reconcile` (pure).** It runs on the chronological rows in memory, in the
worker, before the insert. It checks the statement *as the bank printed it* and
does not depend on what is already stored:
- Each row is checked against the balance **printed on the row before it**, not
  against a running total. One misread value then points at its own spot instead
  of making every later row wrong.
- The first row is checked against the opening balance, and the totals against the
  closing balance, only when the parser knows them. (ICICI prints neither, and the
  parser derives both from the rows. So for ICICI these two checks can only fail
  when a link is broken too.)
- Issue kinds: `balance_mismatch`, `missing_balance`, `opening_balance_mismatch`,
  `closing_balance_mismatch`, each with the **file row number**. There are no
  amounts or balances in an issue, which is stored and returned (CLAUDE.md rule 3).

**4. Outcome.** The issues are stored as `statement_uploads.reconciliation_issues`
(JSONB, at most 100; `reconciliation_errors` keeps the full count). The status is
`needs_review` if there are any issues, otherwise `completed`. The data is imported
either way: the user decides what is wrong. `reconciliation_issues` is **NULL when
nothing could be checked** (no balance column) and `[]` when it was checked and is
clean. "Could not check" and "checked, fine" are different statements, and the UI
should be able to say which one it is.

## Alternatives rejected

**Sort a day's rows by something else (amount, narration, id).** Deterministic,
but wrong: it invents an order, and the balance column would still not add up from
row to row.

**Store the file's row number instead of a chronological position.** It is
meaningless across export directions: in a newest-first file, row 1 is the latest.
`statement_seq` means "position in time" for every file.

**Reconcile against the stored transactions.** Rows skipped as duplicates belong to
another upload, so the stored rows of an upload have gaps. The statement in memory
is complete, and it is what §6.4 is about. (An earlier draft of the Phase 2 plan
said reconciliation needed the stored order; it does not.)

**Refuse a statement that does not add up.** It would lose real data over what may
be a bank's own rounding, or a single bad row. SPEC §6.4 says keep it and flag it.

**Back-fill old rows from the file.** The file is deleted after import (SPEC §7.3).
Rows imported before this step get a deterministic `row_number()` by
`(txn_date, id)`, the order they were already listed in. Re-importing gives them
their real order.

## Consequences
- One day's transactions are listed in the statement's order, and the balance
  column reads correctly from row to row. The real ICICI statement (175 rows)
  reconciles with **0 issues**.
- Two uploads' rows on the same day (only where two statements overlap) have
  positions that cannot be compared. They are in an arbitrary but fixed order
  (`id` breaks the tie), so paging stays exact.
- Clients holding an old cursor get one 422 and start from page 1.
- `needs_review` uploads block the same file like completed ones (ADR 011): their
  rows are imported.
- A newest-first ICICI export has no derived opening/closing balance (the parser
  refuses to guess). Its rows are still checked link by link after reversal.
