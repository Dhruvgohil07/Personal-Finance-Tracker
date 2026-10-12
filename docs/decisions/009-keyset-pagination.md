# ADR 009 — List endpoints page by opaque keyset cursor

- Status: accepted
- Date: 2026-10-12 (Phase 1, Step 8)

## Context
`GET /transactions` is the endpoint the whole app is read through, and it is
the first list endpoint big enough to need paging: one month of a real ICICI
statement is 175 rows, and a year will be a few thousand. SPEC §10 already
says "List endpoints use keyset (cursor) pagination on `(txn_date, id)`", so
this ADR records *how* and *why*, because the same mechanism will be reused
by `GET /uploads`, `GET /alerts` and the insights lists later.

Two properties matter for this data:

1. **The list grows at the newest end.** Every import adds rows at the top,
   and the user usually reads from the top. Anything that numbers pages from
   the start therefore shifts under the reader.
2. **Rows share a date.** `txn_date` is a DATE, so a single day can hold a
   dozen transactions. The sort key must be unique, or a page boundary
   inside one day is undefined.

## Decision
Order by `(txn_date DESC, id DESC)` and page with a cursor that holds that
key:

```
GET /transactions?limit=50                       -> items + next_cursor
GET /transactions?limit=50&cursor=<next_cursor>  -> the next page
```

- The SQL condition is a row comparison,
  `WHERE (txn_date, id) < (cursor_date, cursor_id)`, which is exactly how
  `ix_transactions_user_txn_date_id (user_id, txn_date DESC, id DESC)` is
  sorted.
- `id` is part of the key, not decoration: it breaks ties within a day.
- The service reads `limit + 1` rows. If the extra row exists there is a
  next page, and `next_cursor` is built from the last row of the page; the
  extra row is dropped. `next_cursor` is `null` on the last page, which is
  the client's "stop" signal.
- The cursor is `base64url("<ISO date>|<uuid>")` with the padding stripped
  (`app/utils/cursor.py`), and it is treated as **opaque**: the client only
  ever sends back what we gave it. Anything else is a 422 naming `cursor`.
- The cursor is *not* a capability. `WHERE user_id = :caller` is applied
  regardless, so a cursor from another user only moves the reading position
  inside your own rows (tested in `test_a_cursor_never_reaches_another_users_rows`).

Measured on 20 000 synthetic transactions (`EXPLAIN (ANALYZE, BUFFERS)`),
reading the page at position 19 000:

| | plan | rows read | buffers | time |
|---|---|---|---|---|
| `(txn_date, id) < cursor` | Index Scan, the cursor inside `Index Cond` | 50 | 15 | 0.35 ms |
| `OFFSET 19000` | Index Scan, then discard | 19 050 | 2 753 | 3.33 ms |

Both use the index; only the cursor lets Postgres *start* at the right
place. The gap grows with the table, which is the whole point.

## Alternatives rejected

**`LIMIT/OFFSET` with page numbers.** Simplest to implement and it allows
"jump to page 7". Rejected for the two properties above: the deep page costs
grow with the table (the measurement), and an insert while the user is
paging shifts every later row down, so the next page silently repeats one
row and skips another. For a list that is appended to by every import, that
is a correctness problem, not a performance one.

**A cursor of `txn_date` only.** Smaller and human-readable. Rejected
because a day holds many transactions: resuming at a date either repeats
the whole day or skips the rest of it. Any unique tie-breaker would do;
`id` is already in the index.

**Expose the key as two plain parameters** (`?after_date=&after_id=`).
Honest and debuggable. Rejected because it freezes the sort key into the
API: adding a tie-breaker or paging a differently sorted list later would
mean new parameters and a client that must keep them in step. One opaque
string keeps that freedom, and the 422 on a hand-made cursor is what tells
a client it was never meant to build one.

**Sign or encrypt the cursor.** Rejected: it protects nothing. The contents
are a date and an id of the caller's own row, and the scoping that actually
matters (`user_id`) is not read from the cursor at all.

## Consequences
- Clients cannot jump to an arbitrary page or show "page 3 of 20". The UI
  gets "load more" / infinite scroll instead, which is what the frontend
  (Phase 3) was going to do with a transaction list anyway.
- No total count is returned. A `COUNT(*)` over the same filters would be a
  second query that can disagree with the page it describes; if the UI ever
  needs a total, it gets its own endpoint.
- Filters must be sent again with the cursor. The cursor carries a position,
  not a query, so a client that changes a filter must start from the first
  page (covered by `test_filters_still_apply_on_later_pages`).
- Changing the sort order of an endpoint invalidates cursors already in
  flight. A stale cursor can only produce a wrong *position*, never wrong
  rows, because the user scoping is independent of it.
- `app/utils/cursor.py` is pure (no FastAPI, no SQLAlchemy), so the next
  list endpoint reuses it as it is.
