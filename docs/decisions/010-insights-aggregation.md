# ADR 010 — Insights are hand-written aggregate SQL, and what the plans showed

- Status: accepted
- Date: 2026-10-13 (Phase 1, Step 9)

## Context
SPEC §9.1 asks for the insights queries to be "clear SQL (GROUP BY,
date_trunc, window functions where useful)", and to be verified with
`EXPLAIN ANALYZE` against the indexes, with the findings recorded in an ADR.
This is that ADR. Phase 1 builds two of the four insights endpoints:

- `GET /insights/summary?month=YYYY-MM` — income, spend, net, and the
  change against the previous month
- `GET /insights/categories?from&to` — spending per category

Everything else in the app reads rows through the ORM. These two read
*numbers*, which is a different problem, and several smaller decisions
follow from it.

## Decision

### 1. The two queries are hand-written SQL in `text()`, with bound parameters
`SUM(...) FILTER (WHERE direction = :credit)` over a `GROUP BY
date_trunc('month', txn_date)` is a sentence you can read out loud. The
SQLAlchemy Core equivalent is a stack of `func.sum(case(...))` calls that
nobody can, and it cannot be pasted into `EXPLAIN ANALYZE` without being
rendered first.

This is not a hole in CLAUDE.md §7.3 ("parameterized queries only"). The
SQL strings are module-level constants, identical for every user and every
request; every value — the user id, the two dates, even the `'debit'` /
`'credit'` constants — travels as a bound parameter. Nothing is formatted
into the query, so there is no f-string to get wrong later.

### 2. Self transfers are excluded by the BOOLEAN, not by the category kind
`WHERE NOT is_self_transfer`, never `JOIN categories ... WHERE kind <>
'transfer'`. The column is the one SPEC §9.1 names and the one Phase 2's
automatic detection (§8.4) will write. The two cannot drift apart, because
Step 8 made `PATCH /transactions/{id}` derive the flag from the chosen
category's kind.

### 3. Growth from zero has no percentage: `null`, not 0 and not 100
Every number is infinitely larger than zero, so there is no honest
percentage for "₹0 last month, ₹5,000 this month". `0` would draw a flat
line where there is no line and `100` would invent a comparison, so the
field is `null` and the client says "no comparison" in words. There is no
percentage for `net` at all, because a value that can cross zero cannot
have a meaningful percentage change (-₹500 → +₹500 is not "-200%").

### 4. Uncategorized spending is a row, not a hole
The breakdown uses `LEFT JOIN categories`, so transactions with
`category_id IS NULL` come back as one bucket named "Uncategorized" with a
`null` id. An inner join would have been shorter and would have made the
parts stop adding up to the total — and in Phase 1, where nothing is
categorized automatically yet, it would have made the endpoint look empty
instead of honest.

The seed also contains a system category *named* "Uncategorized" (SPEC §8's
explicit "I looked and found nothing" answer). A row filed there has a real
id, so the two appear as separate rows. That is the truthful rendering of
two different facts; revisit the naming in Phase 2, when the categorizer
actually starts using the seeded one.

> **Amendment (Phase 2, Step 6):** the NULL bucket is now named
> **"Not categorized yet"**, so it no longer shares a name with the seeded
> "Uncategorized" category (ADR 016 §7). The categorizer leaves rows it cannot
> place as NULL, so the bucket keeps meaning "needs your attention". The unscoped
> `LEFT JOIN categories` is still safe only while every stored `category_id` is a
> system category or the row owner's own. Since Step 6 that is also checked where
> the categorizer writes the id (ADR 016 §6).

### 5. `SUM()` of a BIGINT column is NUMERIC — convert in Python, not in SQL
Postgres widens `SUM(bigint)` to `numeric` so a total can never overflow a
single row's type. The driver therefore returns `Decimal`, and `format_inr`
rejects anything that is not an `int` (ADR 001), so an unconverted value
would have been a 500. `int(row.income_paise)` at the boundary keeps the
overflow safety (Python ints are unbounded); a `::bigint` cast in the query
would have been shorter and would have thrown it away.

### 6. Shares are computed in Python, not with a window function
`SUM(...) OVER ()` would give the grand total in SQL. The rows are already
in memory, so the division is done there instead: the percentages and the
total are then derived from the same numbers and cannot disagree. Shares
are rounded half-up to one decimal place, which means three equal
categories report 33.3% each and add to 99.9% — documented in the tests
rather than papered over by adjusting one row.

## Measurements

Method: 40 800 transactions in the test database — two users × 20 400 rows
(24 months × 850), roughly 1 row in 12 a credit, 1 in 40 a self transfer,
two thirds categorized — then `ANALYZE`, then `EXPLAIN (ANALYZE, BUFFERS)`
on the statements the service issues, warmed once first.

| Query | Plan | Buffers | Time |
|---|---|---|---|
| `/insights/summary` (2 of 24 months) | Bitmap Index Scan on `ix_transactions_user_txn_date_id`, then HashAggregate to 2 rows | 78 | **0.88 ms** |
| `/insights/categories` (1 month) | Index Scan on `ix_transactions_user_txn_date_id`, Hash Left Join to 25 categories, HashAggregate to 26 rows | 824 | **0.69 ms** |
| `/insights/categories` (12 months) | Bitmap Index Scan on `ix_transactions_user_merchant` (user_id only), dates filtered from the heap | 699 | **8.75 ms** |

Both narrow queries get the whole `WHERE` into the index condition:

```
Bitmap Index Scan on ix_transactions_user_txn_date_id
  Index Cond: ((user_id = '...'::uuid)
           AND (txn_date >= '2026-07-01'::date)
           AND (txn_date <= '2026-08-31'::date))
```

so the keyset index built in Step 7 for `GET /transactions` serves the
insights too, and no new index is needed.

### Finding: the 12-month breakdown stops using the dates
At a whole year — 45% of the user's rows — the planner gives up on
narrowing by date and simply reads everything for that user through a
`user_id`-prefixed index, discarding 11 220 of 20 400 rows from the heap.
That is a reasonable choice at that selectivity, and 8.75 ms is fine at
personal-finance size, so nothing is being changed for it. It is recorded
because it is where this endpoint would start to hurt first.

### Finding: `ix_transactions_user_category_date` does NOT serve the breakdown
Step 7 added `(user_id, category_id, txn_date)` with the comment "spend per
category over a period (SPEC §9.1 insights)". That is wrong, and this is
the first time it could be measured. The breakdown **groups by** category
rather than filtering to one, so the index's second column is
unconstrained and the date predicate behind it can never become an index
condition. Postgres never picks it — not even with `enable_bitmapscan` and
`enable_seqscan` turned off, where it still prefers the narrower
`ix_transactions_user_merchant` (9.55 ms).

The index is kept, because it does serve a real query — Step 8's
`GET /transactions?category_id=…&from=…&to=…`, where all three columns land
in the index condition:

```
Index Scan Backward using ix_transactions_user_category_date
  Index Cond: ((user_id = '...') AND (category_id = '...')
           AND (txn_date >= '2025-09-01') AND (txn_date <= '2026-08-31'))
  Buffers: shared hit=54                      Execution Time: 0.110 ms
```

The misleading comment in `app/models/transaction.py` has been corrected.
This is the second index whose purpose only survived contact with
`EXPLAIN` in an edited form — the first was the trigram index in Step 8 —
and the lesson is the same: an index comment is a claim, and a claim
should be measured.

## Alternatives rejected

| Alternative | Why not |
|---|---|
| Build the aggregates through SQLAlchemy Core | Unreadable for exactly the queries that most need to be read, and SPEC §9.1 asks for clear SQL |
| Two separate queries, one per month | Two round trips and two index scans over rows that sit next to each other in the same index; one widened range answers both |
| Report `0%` when the previous month was empty | States a comparison that was never possible (see decision 3) |
| Inner join and drop uncategorized spending | The parts would stop adding up, and Phase 1 would look empty rather than honest |
| Cast `SUM(...)::bigint` in SQL | Throws away the overflow protection Postgres gives for free |
| Add an index for the 12-month breakdown | Nothing to index: the query groups by category over the user's whole range. 8.75 ms at 20 000 rows does not justify a covering index |
| Make `month` and `from`/`to` required | A dashboard's first request has no parameters; "the current month in IST" is the obvious default (CLAUDE.md rule 10) |

## Consequences
- The insights add **no new index and no new migration**. They ride on
  `ix_transactions_user_txn_date_id`.
- `GET /insights/trend?months=12` (SPEC §10) is the same monthly query with
  a wider range, so it is a small addition in Phase 2 rather than new work.
  `GET /insights/top-merchants` needs `transactions.merchant_id` to be
  filled, which is the Phase 2 categorizer's job.
- Changing what "spending" means — excluding a category kind, say — now has
  to be changed in SQL in two places. Both are in one module, and both are
  covered by tests whose totals are written out by hand.
- The percentages round half-up to one decimal place, so a breakdown's
  shares need not add to exactly 100.0. `total_spend_paise` is the exact
  figure a client should use for arithmetic.
- `Month` (`app/utils/dates.py`) is a pure value object with its own tests.
  The trend endpoint, budgets (`GET /budgets/status?month=`) and the
  recurring-payment cadences all need the same calendar arithmetic.
