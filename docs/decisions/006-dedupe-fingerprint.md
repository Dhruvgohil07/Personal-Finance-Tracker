# ADR 006 — Idempotent imports: file hash + per-row fingerprint

- Status: accepted
- Date: 2026-10-03 (Phase 1, Step 5)

## Context
Bank statements are downloaded by date range, by hand, so the same
transaction reaches us more than once:

- the user uploads the same file twice (a double click, or "did that
  work?");
- the user downloads 1–31 August, then later 15 August – 15 September, so
  two weeks exist in both files;
- a statement is re-downloaded after the bank adds a late-posting row.

Duplicated transactions would corrupt every number the app exists to
produce: monthly spend, budgets, category breakdowns, recurring-payment
detection. CLAUDE.md rule 5 therefore states that imports must be
idempotent: uploading the same statement, or overlapping statements, must
never create duplicate transactions.

Rows also cannot be identified by anything the bank gives us. Indian
statements have no stable per-transaction id, narration wording can differ
in spacing or case between downloads, and a reference number is not always
present.

## Decision
Two independent levels, as SPEC §6.3 describes.

**Level 1 — the whole file.** `statement_uploads` has
`unique(user_id, file_sha256)`. Re-uploading a byte-identical file is
rejected with 409 before anything is parsed. Cheap, and it catches the
most common case.

**Level 2 — each row.** Every transaction gets a `fingerprint`: the hex
sha256 of its identifying fields joined with `|`.

```
account_id | txn_date | amount_paise | direction |
normalized_description | balance_after_paise | occurrence_index
```

`transactions` has `unique(account_id, fingerprint)`, and Step 7 inserts
with PostgreSQL `INSERT ... ON CONFLICT (account_id, fingerprint) DO
NOTHING`. Rows already present are skipped and counted as duplicates;
genuinely new rows go in. One statement for the whole file, no
"does this row exist?" query per row, and it stays correct when two
imports run at once, because the database decides.

Implemented as pure functions in `backend/app/parsers/fingerprint.py`
(`compute_fingerprint`, `assign_occurrence_indexes`, `fingerprints_for`),
unit tested in `backend/tests/unit/test_fingerprint.py`.

### Field-by-field reasoning
- **account_id** — the same transaction can legitimately exist in two of
  the user's accounts (a transfer between them appears in both
  statements). Those are two real rows.
- **txn_date, amount_paise, direction** — the core identity of a payment.
  Integer paise, so the comparison is exact (ADR 001).
- **normalized_description** — not the raw narration. The same
  transaction may be printed with different spacing or case in two
  downloads, so the fingerprint uses the normalized form
  (`app/parsers/normalize.py`): uppercased, whitespace collapsed, runs of
  six or more digits removed.
- **balance_after_paise** — the strongest tie-breaker when the statement
  has a balance column: two equal payments on one day have different
  balances afterwards. Empty string when absent, never the text `"None"`,
  so a narration containing that word cannot collide with a missing value.
- **occurrence_index** — the Nth identical row on that date within the
  statement (0, 1, 2 …). Without it, two genuine ₹20 chai payments on the
  same day in a statement with no balance column would hash identically
  and the second would be dropped as a duplicate, losing real money.

### Supporting decisions
- **Normalization strips pipes.** The fingerprint joins fields with `|`,
  so a pipe inside a narration could blur the boundary between the
  description and the fields after it. `normalize_description` turns pipes
  into spaces, which removes the ambiguity at the source instead of making
  the hash logic clever.
- **`value_date` is excluded.** Some statements print it and some do not;
  including it would make the same transaction hash differently depending
  on which download it came from.
- **A hash column, not a seven-column unique constraint.** One narrow
  b-tree index instead of a wide one; no `NULL` in the key (in SQL
  `NULL != NULL`, so a missing balance would defeat the constraint); and
  the rule is computed in Python where it is unit tested, rather than
  depending on how Postgres compares seven values of mixed types.
- **sha256, not a shorter hash.** It is used as a content identifier, not
  as password protection (passwords use argon2id). Collisions are not a
  practical concern, and 64 hex characters per row is cheap.

## Alternatives rejected
- *Only the file hash (level 1).* Does nothing for overlapping date
  ranges, which is the common case when a user catches up on a few months.
- *A bank reference number as the key.* Not present in every statement or
  every row, and not stable across re-downloads. Would silently fail for
  cash, charges and interest rows.
- *`(account_id, txn_date, amount_paise, direction)` as the key.* Treats
  two genuine identical payments on one day as one transaction. Exactly
  the case `occurrence_index` exists for.
- *Checking each row with a SELECT before inserting.* One query per row,
  and still wrong under concurrency (two imports can both see "not
  there"). `ON CONFLICT` pushes the decision to the one component that
  can make it correctly.
- *Fuzzy matching (same amount and date, similar narration).* Can merge
  two real transactions, is impossible to explain to a user, and makes the
  result depend on a similarity threshold. Idempotency must be exact.
- *Including the raw narration in the hash.* A spacing change in a
  re-download would create a duplicate.

## Consequences
- Re-uploading a file is rejected instantly; overlapping statements insert
  only their new rows, and the upload reports
  `rows_parsed / rows_inserted / rows_duplicate`.
- Normalization is now part of the dedupe contract. **Changing
  `normalize_description` changes every future fingerprint**, so stored
  rows would no longer match newly parsed ones and an overlapping upload
  could re-insert old transactions. Phase 2 will refine normalization from
  real narrations, so it needs a one-off migration that re-computes
  fingerprints for existing rows (`normalized_description` is stored, so
  it can be recomputed). Noted as a Phase 2 task.
- Stripping long digit runs makes more rows look identical (two payments
  that differed only by reference number). That is deliberate: it keeps
  merchant keys usable, and `occurrence_index` keeps the rows distinct.
- **Known edge case.** `occurrence_index` is assigned per statement, so it
  is only stable if identical rows appear together in both files. A
  statement period that begins *between* two identical rows on the same
  day — the first file ends with chai #1, the second starts with chai #2 —
  gives chai #2 the index 0 in the second file, matching chai #1's
  fingerprint, and it is skipped as a duplicate. In practice statements
  are cut at day boundaries, so both copies appear in both files; and when
  a balance column exists the two rows are already distinct. Accepted for
  now: the failure mode is one missing row in a rare case, never a
  duplicate or a wrong total. If it shows up with real data, the fix is to
  anchor the index to the statement's own day boundary rather than the
  file.
- The import is append-only: a transaction the bank later *removes* from a
  re-issued statement stays in our database. Deleting transactions is a
  user action, not an import side effect.
