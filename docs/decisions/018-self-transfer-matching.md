# ADR 018 — Self-transfer matching

- Status: accepted
- Date: 2026-10-27 (Phase 2, Step 8)

## Context
Paying your own credit card from savings, or moving money between two of your bank
accounts, appears twice: a debit in one account and a credit in the other. Counted as
spending and income, it inflates both. SPEC §8.4: a debit in one of the user's
accounts and a credit of the same amount in another, within 0–2 days, mark both
`is_self_transfer = true`, category Self Transfer; SPEC §9.1 excludes them from the
totals (the insights queries already read the flag).

ADR 017 made one constraint hard: every rule change re-runs the chain over all of the
user's non-manual rows, so a flag set once by a separate detector would be undone by
the next rule change.

So far only one real account (ICICI savings) is imported. The step is verified with
synthetic two- and three-account statements only.

## Decision

**1. A pure matcher, `app/categorizer/self_transfer.py`.** It takes rows (id,
account, direction, amount, date) and returns `(debit id, credit id)` pairs.
- A pair is a debit in account A and a credit of the same amount in account B, with
  B different from A and the credit 0–2 days after the debit.
- Matching is one-to-one and goes gap by gap (0, then 1, then 2 days). An edge
  becomes a pair only if both of its rows have no other edge at that gap. Every other
  row with an edge at that gap is closed, unmatched.
- A row that is ambiguous at its nearest gap is not tried farther away. Its true
  partner is most likely one of the near candidates.
- The result does not depend on row order (tested over all permutations).
- It is the first part of the categorizer that sees amounts and dates. It only
  compares them, and the chain's `TxnFacts` stays amount-free.

**2. Detection runs at the end of every categorization pass** (decided with Dhruv).
The passes are the import's `categorize_upload` (same transaction) and the rule
re-run `recategorize_user`. Both run under the same per-user lock, and detection looks
at all of the user's accounts. The invariant: *a non-manual row always shows "the
chain, then detection"*. A rule re-run therefore re-creates the pairs instead of
undoing them. No separate job is needed, and `post_import_analysis` is left to Phase 4
recurring detection.

**3. Which rows may be paired** (decided with Dhruv):
- Not manual rows, because the user decided them.
- Not rows a user rule decided, because the user said so. A rule on one half leaves
  the other half unpaired.
- Everything else, including the chain's built-in guesses. A payment to your own
  account looks exactly like a P2P transfer to a person, so detection must be able to
  override that guess.

**4. Both rows of a pair** get category Self Transfer, source `heuristic`,
confidence **0.80**, and `is_self_transfer = true`. 0.80 sits below a rule's 1.00 and
above the P2P guess's 0.70. A row is "detected" exactly when it is `heuristic` and
`is_self_transfer`; nothing else writes that combination.

**5. A detected row that loses its pair is re-decided by the chain.** For example, a
new import adds a second credit of the same amount on the same day, so the pair is
now ambiguous. In the re-run pass the chain already does this; in the import pass
detection does it explicitly.

**6. Re-filing either half by hand re-runs the user's rows** (decided with Dhruv).
`PATCH /transactions/{id}` that changes the category of a detected row goes through
`commit_and_apply`, the same path as a rule change. Its partner is re-decided at once
rather than staying out of the totals. A note, or a change to any other row, does not
re-run anything.

**7. No pair link is stored** (decided with Dhruv). There is no
`self_transfer_pair_id` column and no migration. The pairing is recomputed on every
pass anyway.

## Alternatives rejected
- **The plan's separate `post_import_analysis` job.** Rows would be briefly
  un-flagged after every import and every rule re-run. It would also need its own
  enqueueing from both places and its own Redis-down path.
- **Detection only on uncategorized rows.** A card payment the P2P heuristic had
  filed as "Transfers to People" would never be detected, and that is the common case.
- **Detection overriding user rules too.** A rule exists because the user decided.
- **Absolute date difference (the credit up to 2 days before the debit).** SPEC §8.4
  describes the money leaving one account and arriving in the other. Revisit if a real
  bank posts the credit side earlier.
- **Falling back to a farther candidate when the nearest is ambiguous.** That gives a
  confident answer where the data allows none.
- **A pair link column.** It adds a migration and something to keep in sync. Phase 3
  can add it if the UI needs "paired with…".

## Consequences
- Every import now locks and reads all of the user's pairable rows, not only the new
  ones. That is a few thousand rows, which is fine at personal-finance size.
- Two of your accounts receiving the same amount on the same day (two salaries of
  equal value, say) make the debit ambiguous, so nothing is paired. This is
  conservative by design: the user can re-file by hand.
- Only `.xls` and CSV imports exist so far, so pairs across a savings account and a
  credit-card statement wait for a credit-card parser (no sample yet, SPEC §6.2).

## Amendment — code review of PR 23 (7 findings, all addressed)
Four of these were decided with Dhruv (marked *decided*).

1. **Manual Self Transfer rows are anchors** (*decided*). This replaces point 3's "not
   manual rows". A row the user filed under Self Transfer takes part in matching as a
   fixed half. Its automatic partner is paired to it, and the anchor itself is never
   written. Before, confirming one half by hand stripped the other half of its pair,
   and a transfer marked by hand never found its other half. This extends point 6: a
   category change re-runs the user's rows whenever the row is a self transfer
   **before or after** the change.
2. **Only uncategorized (`none`) and `heuristic` rows are pairable** (*decided*). This
   narrows point 3's "everything else". Rows placed by the merchant dictionary, a
   keyword or (Phase 4) the LLM are never paired. Before, rent of ₹25,000 and a
   ₹25,000 salary a day later in another account erased each other from the totals.
   No payment app is a seeded merchant, so card-bill payments through them still
   pair.
3. **Deleting an upload or an account that holds half of a pair re-runs the user's
   rows** once the delete is committed. Before, the other half stayed Self Transfer,
   out of the totals, with no partner. The re-run follows the commit rather than
   sharing its transaction: a delete cascades into rows a running import or re-run
   may hold, so taking the categorization lock inside it could deadlock.
4. **Lock order: the per-user lock first, rows after.** A category change takes the
   lock before reading the row. Before, with Redis down, the inline re-run took it
   while already holding the edited row, which is the reverse of every run's order
   and so a possible deadlock.
5. **Clearing a detected half may re-pair it** (*decided*, behaviour kept).
   `category_id: null` means "let the categorizer decide" for every row. `/docs` now
   says so, and that choosing another category is how a pair is rejected.
6. **The import pass reads only rows whose amount a new row has.** This is exact,
   because a pair only links equal amounts, so each amount pairs independently.
   Before, every import read and locked every pairable row of the user.
7. **`commit_and_recategorize` moved from the rules service to the categorization
   service** (formerly `commit_and_apply`). It is the general "commit, then re-run"
   mechanism, now used by rules, by re-filed pairs and by deletes
   (`recategorize_after_commit`).
