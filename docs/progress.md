# Progress

## Current phase
Phase 1 — Backend MVP (plan: docs/plans/phase-1.md)

## Completed
- [x] Phase 0 — Foundation (config, logging, errors, money, DB + seed, /health, CI; plan: docs/plans/phase-0.md)
- [x] Phase 1 plan approved — docs/plans/phase-1.md
- [x] Step 1: Users + security helpers — users/refresh_tokens models, migration 0002
  (incl. categories.user_id FK), app/core/security.py (argon2id, access JWT, refresh token HMAC)
- [x] Step 2: Auth endpoints (register/login/refresh/logout), `get_current_user`, ADR 005
- [x] Step 3: Rate limiting (slowapi + Redis): login/register 5/min/IP, general
  100/min/user, `UPLOAD_LIMIT` ready for Step 7; ADR 007
- [x] Step 4: Accounts CRUD — model + migration 0003, GET/POST/PATCH/DELETE /accounts,
  IDOR tests, `make_auth_headers` test fixture; review fixes: account_type in the unique
  key (migration 0004), 409 (not 500) when PATCH collides with that key, only the matching
  unique constraint maps to 409, no control characters in names/nicknames
- [x] Step 5: Pure parsing core — `app/parsers/` base types (FileType/Direction/ParserInput/
  RawRow/NormalizedRow/ParsedStatement/ParseError/StatementParser Protocol), normalization,
  fingerprint + occurrence index, ParserRegistry; ADR 006; 83 unit tests, 100% coverage of
  the package, and a test asserting the package imports no FastAPI/SQLAlchemy/Redis
- [x] Step 6: File readers + parsers — `app/parsers/readers.py` (signature-based file type
  detection, CSV with BOM/encoding/delimiter handling, `.xls` via new dependency `xlrd`),
  `generic_csv.py` (`ColumnMapping` + `GenericCsvParser`), `icici.py` (`IciciXlsParser`),
  `build_default_registry()`; synthetic `.xls` fixture builder (dev dependency `xlwt`);
  ADR 004; 99% coverage of `app/parsers`. Verified on the real statement: 175 rows,
  148 debits / 27 credits, period read from the header, 175 distinct fingerprints,
  opening − debits + credits == closing exactly

- [x] Step 6 code review (`/code-review high`, 10 findings) — all 10 fixed on the branch:
  ICICI truncation guard (`_check_table_really_ended`), derived balances refused when rows
  are not oldest-first, `account_last4` taken only from the END of the account identifier
  (masked tail → None), reversed header period reported as unknown instead of failing the
  import, `ColumnMapping` rejects a column mapped to two roles, CSV reader handles UTF-16
  and refuses text containing NUL; a text file named `.xls` now says what is actually wrong;
  an ICICI statement with no transactions returns an empty `ParsedStatement` instead of
  failing (a dormant month is quiet, not corrupt); `GenericCsvParser` joins description-only
  continuation rows like the ICICI parser; `build_icici_xls` gained `start_column` so the
  label-based column lookup is actually tested. Real statement re-verified unchanged
  (175 rows, same period/last4/balances/fingerprints); 417 unit + 95 integration tests pass

- [x] Step 7: Uploads + import service — `statement_uploads` + `transactions` models (full §5
  shape, keyset + trigram GIN indexes), migration 0005, `app/services/imports.py`
  (size → ownership → sha256 → parse → account-number check → normalize → fingerprint →
  one transaction with `ON CONFLICT DO NOTHING`), `POST /uploads` (multipart, 10/hour/user),
  `ColumnMappingRequest`, errors `PAYLOAD_TOO_LARGE` + `INVALID_STATEMENT`; ADR 008.
  576 tests pass, 99% coverage. Verified on the real statement: 175 rows imported, and a
  re-import of the same rows counts 175 duplicates and inserts 0

- [x] Step 8: Transactions list + manual categorization + categories — `GET /transactions`
  (10 filters from SPEC §10, keyset pagination on `(txn_date DESC, id DESC)` with an opaque
  base64 cursor), `PATCH /transactions/{id}` (category + notes, `category_source =
  user_manual`), `GET /categories` (system + own, `is_system`); new `app/utils/cursor.py`
  (pure), `app/schemas/transaction.py` + `category.py`, `app/services/transactions.py` +
  `categories.py`; ADR 009. 697 tests pass, 99% coverage (100% of the new modules).
  Verified with `EXPLAIN (ANALYZE, BUFFERS)` on 20 000 rows: the cursor lands inside the
  `Index Cond` of `ix_transactions_user_txn_date_id` (50 rows, 15 buffers, 0.35 ms) while
  the same page via `OFFSET 19000` reads 19 050 rows (2 753 buffers, 3.33 ms). Re-checked
  on the real ICICI statement: 175 rows imported, read back in 9 pages of 20 with 175
  unique ids newest-first, 148 debits / 27 credits, the whole-period date filter returns
  all 175, and a manual categorization shows up in `?category_id=`

- [x] Step 9: Insights — `GET /insights/summary?month=YYYY-MM` (income, spend, net, the
  previous month and the % change) and `GET /insights/categories?from&to` (spend per
  category, biggest first, with an "Uncategorized" bucket); new `app/utils/dates.py`
  (pure: fixed-offset IST clock + a `Month` value object), `app/schemas/insights.py`,
  `app/services/insights.py` (hand-written aggregate SQL via `text()`, every value bound),
  `app/api/v1/insights.py`; Step 8's "unknown param = 422, blank = not sent" validator
  extracted into a shared `QueryFilters` base in `app/schemas/common.py`; ADR 010.
  837 tests pass, 99% coverage (100% of all five new/changed modules).
  Verified by MUTATION: dropping `NOT is_self_transfer` fails 6 tests, dropping the
  `user_id` filter fails the IDOR test, making the range end exclusive fails 3 boundary
  tests, and `LEFT JOIN` → `JOIN` fails 8 — so the totals are not asserted against
  whatever the SQL happens to return. Measured with `EXPLAIN (ANALYZE, BUFFERS)` on
  40 800 rows (2 users × 24 months): summary 0.88 ms / 78 buffers and a 1-month
  breakdown 0.69 ms, both with user_id + both date bounds inside the `Index Cond` of
  `ix_transactions_user_txn_date_id`; a 12-month breakdown is 8.75 ms

## In progress
- (nothing yet)

## Next up
- [ ] Step 10: End-to-end check with the real ICICI statement, README walkthrough

## Open decisions / questions
- Git workflow → decided: `main` protected (ruleset `protect-main`); every change via a
  feature branch + PR, green `backend` check required, rebase-merge only (CLAUDE.md §18)
- RQ worker on Windows → decided: run as a Docker service (added in Phase 2)
- Schema scope → decided: add tables phase by phase
- Log redaction → decided: substring match on normalized keys; `email` is redacted (log user_id instead)
- Money parsing → only empty/whitespace cells are blank; "-" as blank is undecided until real statements are seen
- TestClient HTTP library → decided: dev dependency `httpx2` replaces `httpx` (Starlette deprecated httpx)
- /health 503 body → decided: same HealthResponse shape as 200 (not the error shape), so
  monitors can see which check failed
- /health DB timeout → decided: separate NullPool `health_engine` with a 3 s connect
  timeout (app keeps 10 s for Neon wake-ups); worst-case 503 now ~5 s instead of ~12 s
- Request logging → decided: uvicorn access log off; own middleware logs path without
  query string + request_id (X-Request-ID header) (ADR 003)
- DB conventions → decided: VARCHAR+CHECK enums, NULLS NOT DISTINCT, categories.user_id FK added in 0002 (ADR 002)
- CI secrets → decided: random throwaway JWT/encryption secrets generated per run
  (openssl), none committed; test DB created with the same init SQL as docker-compose
- Category kinds → open: "Transfers to People", "Investments", "Refunds" and "Uncategorized" kinds
  are a first guess; revisit when building insights (§9.1). Changing them = edit seed_data.py + re-seed
- Merchant key format → open: confirm against real narrations in Phase 2 (§6.5). Step 5
  builds a first-pass candidate: drop noise/filler words, pure digits and 1–2 char tokens,
  take the first token left. Known gap: a P2P VPA yields the bank handle ("YBL") until
  Phase 2 adds VPA extraction + P2P detection (§8.3) ahead of the merchant lookup
- Parsing core (Step 5) → decided: `FileType` = csv/xls/pdf (SPEC §5 lists only csv/pdf;
  `xls` added for ICICI, ADR 004 — the Step 7 migration uses these three values);
  `Direction` lives in `app/parsers/base.py` and the Step 7 model imports it from there
  (models may import parsers, never the reverse); `NormalizedRow` wraps `RawRow` instead of
  copying its fields; normalization replaces `|` so it can't blur fingerprint fields;
  registry does NOT fall back to the generic CSV parser (that needs the user's column
  mapping, so Step 7 decides it) and has no module-level instance (Step 6 builds one)
- Fingerprint (Step 5, ADR 006) → decided: sha256 over account_id|txn_date|amount_paise|
  direction|normalized_description|balance_after_paise|occurrence_index; `value_date`
  excluded (not in every statement). Open edge case: `occurrence_index` is per statement,
  so a period starting *between* two identical same-day rows would skip the second as a
  duplicate — accepted (see ADR 006 consequences), revisit if real data shows it
- Changing `normalize_description` later changes every fingerprint → Phase 2 needs a one-off
  migration recomputing fingerprints for stored rows (`normalized_description` is stored)
- ICICI statements are legacy `.xls` → decided: support via new dependency `xlrd` (ADR 004 in Step 6)
- Rate limiting → decided: built in Phase 1 (Step 3), not Phase 5
- Phase 1 extra endpoints → decided: only `GET /categories`; `/me`, upload list/delete and
  user categories CRUD deferred
- Refresh token lifetime → decided: 30 days (not in the spec); each refresh issues a new token
- User `name` → required (NOT NULL); `settings` default filled by the ORM, no DB default
- Register → decided: returns 201 + user, no tokens (client logs in next); duplicate → 409 (ADR 005)
- Logout → decided: revokes the whole token family of that session; no access token needed
- Accounts → GENERIC collisions: two "other" banks with the same last 4 digits and type
  (e.g. Axis + Kotak, both GENERIC savings ...1234) still get 409. Reviewed in Step 7 and
  left as is: fixing it means either adding bank codes (a migration per bank) or a new key,
  and no real file needed it yet. Revisit when the second bank is added (Phase 2)
- Accounts → changing `account_type` after transactions exist (savings <-> credit_card):
  reviewed in Step 7 → left allowed. Nothing in Phase 1 reads `account_type` (the parsers
  take the direction from the statement's own columns), so no stored row changes meaning.
  Revisit in Phase 2/insights, where credit-card statements are interpreted differently
- Accounts → decided (confirmed by Dhruv 2026-10-01): `bank_code` is a VARCHAR+CHECK enum (HDFC/SBI/ICICI/GENERIC; new
  bank = migration); nickname required (1–50); only nickname/account_type are editable
- Rate limiting → decided: key = user id from a valid JWT, else client IP (never
  X-Forwarded-For); general limit counted across the whole API; /health exempt;
  Redis down = fail open (ADR 007)

- Step 7 → decided (ADR 008): a FAILED import writes no `statement_uploads` row in Phase 1.
  The import runs inside the request, so the caller already gets the reason; and because
  `unique(user_id, file_sha256)` would then block that file forever, a recorded failure
  would stop the user retrying after we add PDF support or fix a parser. Only `completed`
  rows exist; the other five statuses are reserved for the Phase 2 worker
- Step 7 → decided: the column mapping is the FALLBACK (SPEC §6.2), used only when no bank
  parser recognises the file, so a real ICICI `.xls` is always read by the ICICI parser even
  if a mapping was sent with it. A mapping + an unrecognised `.xls` is a 400 (a mapping
  names CSV columns)
- Step 7 → decided: the statement's `account_last4` must match the chosen account's
  `masked_number` (400 if not), but `bank_code` is NOT compared — an account added as
  GENERIC may legitimately be the ICICI account the file came from
- Step 7 → decided: transactions are inserted in chunks of 500 rows inside ONE transaction.
  Postgres allows 65535 bind parameters per statement and each row binds ~20, so a single
  INSERT would break somewhere above ~3200 rows (a two-year statement)
- Step 7 → decided: `GET /uploads/{id}` is not built in Phase 1. Processing is synchronous,
  so `POST /uploads` already returns the final status and counts; the polling endpoint
  arrives with the worker in Phase 2
- Step 7 → decided: `merchant_key` from `normalize_row` is computed but not stored.
  `transactions.merchant_id` points at a `merchants` ROW, and looking one up is the
  categorizer's job (SPEC §8), not the importer's
- Step 6 → decided: file type comes from the file's SIGNATURE first (a renamed `.xls` is
  still read as Excel), extension only for CSV; `.xlsx` refused with an actionable message
- Step 6 → decided: numeric `.xls` cells convert via `Decimal(f"{value:.15g}")` (Excel's own
  15-digit precision), so a formula cell reads `0.3`, not `0.30000000000000004`; Excel date
  cells render as ISO and parsers accept ISO as well as their bank's format (ADR 004)
- ICICI narrations → FOUND in the real file: a long narration spills into the next row
  (only the Remarks cell filled; 6 of 181 body rows). Decided: joined by DIRECT
  concatenation, because the cut is mid-token — a separator would change the merchant key
  and every fingerprint (ADR 004)
- ICICI balances → decided: opening/closing are derived from the rows (ICICI prints
  neither); `None` when the statement has no balance column
- ICICI account number → decided: parser returns `ParsedStatement.account_last4` only
  (validated as exactly 4 digits); Step 7 compares it with the chosen account and should
  answer 400 when they differ. The holder's name in the same cell is dropped
- Generic CSV → decided: columns are mapped by HEADER LABEL (case/space-insensitive), so a
  headerless CSV is not supported in Phase 1. Settled in Step 7: `ColumnMappingRequest` is
  label-based only; index-based mapping waits for a real headerless file
- Generic CSV → decided: `ColumnMapping` is a frozen dataclass in the parsers package
  (pure layer) and Step 7 adds the Pydantic request schema that builds it; validation
  raises `ValueError` there and the schema mirrors the same rules for a 422
- Test fixtures → decided: the ICICI `.xls` fixture is GENERATED by
  `tests/fixtures/icici_xls.py` (dev dependency `xlwt`) into `tmp_path`, not committed as a
  binary; no real statement is ever committed (rule 8)

- Step 8 → decided (ADR 009): list endpoints page by an OPAQUE base64 cursor holding
  `(txn_date, id)`, not by `OFFSET`/page numbers. Consequences accepted: no "page 3 of 20",
  no total count, and filters must be re-sent with the cursor. A cursor is a position, not
  a permission — `WHERE user_id = caller` is applied regardless
- Step 8 → decided: `min_amount`/`max_amount` are given in RUPEES (Decimal, max 2 dp,
  converted to paise in the schema); every other money value in the API is integer paise.
  Revisit if the frontend would rather send paise everywhere
- Step 8 → decided: `?account_id=`/`?category_id=` belonging to someone else is a 404, not
  an empty page, so "no rows" never hides "wrong id". Both are looked up through their
  services first
- Step 8 → decided: `q=` searches `normalized_description` only (uppercased, long digit
  runs stripped by §6.5 normalization), using ILIKE with `%`/`_`/`\` escaped. So searching
  an account number finds nothing on purpose, and `notes` are NOT searched — revisit when
  the frontend has a search box. NOTE: the trigram GIN index does NOT serve this query
  (see Known issues); the review of Step 8 measured it
- Step 8 → decided: `GET /transactions` returns `amount_display` (the `format_inr` string,
  rule 10) next to `amount_paise`, and does NOT return `merchant_id` (always null in
  Phase 1, no endpoint exposes merchants yet)
- Step 8 → decided: `PATCH /transactions/{id}` takes only `category_id` and `notes`;
  explicit `null` clears either one, a field left out is untouched (`exclude_unset`), a
  manual category always sets `category_source = user_manual` and clears
  `category_confidence`. `apply_to_similar` (SPEC §10) is a 422 until Phase 2 builds the
  learned rules (§8.1)
- Step 8 → open: no way to list UNCATEGORIZED transactions yet (`category_id` has no "is
  null" form), which is what the Phase 1 manual-categorization flow actually wants. Not in
  SPEC §10's parameter list; decide the spelling (`uncategorized=true`?) in Phase 3 when
  the frontend needs it
- Step 8 → note: category NAME ordering inside a kind is Postgres' collation ("Education"
  vs "EMI & Loans" sort differently there than in Python), so the test asserts the grouping
  by kind only
- Step 8 review → decided: `is_self_transfer` is NOT a field of its own in the PATCH body;
  it is DERIVED from the chosen category's kind — a `transfer` category sets it, any other
  category clears it, clearing the category clears it, and a notes-only PATCH leaves it
  alone. Reason: SPEC §9.1 excludes self transfers by reading the BOOLEAN, so a user who
  files their credit-card payment under "Self Transfer" must not still see it counted as
  spending, and two independent controls could contradict each other. Phase 2's automatic
  detection (§8.4) writes the same column; a manual category is treated as a correction
  of it
- Step 8 review → decided: a BLANK value means "filter not sent" for every filter
  (`?from=&direction=&limit=` behaves like no query string), because a client that
  serializes all of its filter inputs sends empty strings for the boxes the user left
  alone. Only the filters' own names are dropped, so a misspelled parameter is still a 422
- Step 8 review → decided: `category_confidence` is serialized as a JSON NUMBER
  (`PlainSerializer(float)`), not Pydantic's default quoted Decimal string. Rule 1 is
  unaffected — this is a 0.00–1.00 confidence, not money

- Step 9 → decided (ADR 010): the insights aggregates are HAND-WRITTEN SQL in `text()`
  with bound parameters, not SQLAlchemy Core. `SUM(...) FILTER (WHERE ...)` over
  `GROUP BY date_trunc('month', ...)` can be read aloud and pasted into `EXPLAIN ANALYZE`;
  the Core equivalent (`func.sum(case(...))`) can be neither. The SQL strings are
  module-level constants — nothing is formatted in, so §7.3 holds
- Step 9 → decided: self transfers are excluded by `NOT is_self_transfer` (the boolean),
  never by joining categories and testing `kind <> 'transfer'`. Same column SPEC §9.1
  names and Phase 2 §8.4 will write, and Step 8 already keeps it in step with the category
- Step 9 → decided: a % change against a month whose total was 0 is `null`, not 0 and not
  100 — there is no percentage for growth from nothing. `net` gets no % at all, because a
  value that crosses zero cannot have a meaningful percentage change
- Step 9 → decided: the uncategorized bucket (`category_id IS NULL`) is a ROW in
  `/insights/categories`, via `LEFT JOIN`, so the parts always add up to the total. Open:
  it is named "Uncategorized", which is also the name of a SEEDED system category (SPEC §8's
  explicit "looked, nothing fits"), so both can appear as separate rows — distinguishable
  by `category_id` being null. Revisit the naming in Phase 2 when the categorizer starts
  using the seeded one
- Step 9 → decided: `SUM()` over a BIGINT column returns NUMERIC (Postgres refuses to
  overflow silently), so the driver hands back a `Decimal`; it is converted with `int()` at
  the service boundary rather than cast `::bigint` in SQL, which would throw that
  protection away. `format_inr` rejects non-`int`, so an unconverted value was a 500
- Step 9 → decided: `month` and `from`/`to` are OPTIONAL and default to the current month
  **in Asia/Kolkata** (rule 10). `app/utils/dates.py` uses a FIXED UTC+05:30 offset, not
  `zoneinfo.ZoneInfo("Asia/Kolkata")`: India has had no DST since 1945, and a fixed offset
  needs no `tzdata` package on Windows. `from`/`to` are both-or-neither (one alone is a
  422), because defaulting the missing half would silently produce an empty range
- Step 9 → decided: shares and % changes are rounded HALF-UP to one decimal place, so a
  breakdown's shares need not add to exactly 100.0 (three equal categories give 33.3 × 3 =
  99.9). `total_spend_paise` is the exact figure for arithmetic; adjusting one row to cover
  the gap would make that row wrong
- Step 9 → decided: only `/insights/summary` and `/insights/categories` are built.
  `/insights/trend?months=12` is the same monthly query over a wider range (easy in
  Phase 2); `/insights/top-merchants` needs `transactions.merchant_id`, which nothing
  writes until the Phase 2 categorizer
- Step 9 → FOUND by measuring: `ix_transactions_user_category_date` does NOT serve the
  insights breakdown, although Step 7 added it for exactly that. The breakdown GROUPS BY
  category instead of filtering to one, so the index's second column is unconstrained and
  the date predicate behind it can never become an index condition — Postgres ignores it
  even with `enable_bitmapscan`/`enable_seqscan` off. The index IS used in full (all three
  columns in the `Index Cond`, 54 buffers, 0.11 ms) by Step 8's
  `GET /transactions?category_id=&from=&to=`, so it is kept and the misleading comment in
  `app/models/transaction.py` was corrected. Second index in a row whose stated purpose
  did not survive `EXPLAIN` (the first was the trigram index in Step 8)

## Tasks Dhruv implemented (historical — the practice ended 2026-10-12)
From 2026-10-12 Claude completes every task in full; no new `TODO(dhruv)` items are
created (CLAUDE.md §0). Kept as a record of which tests and endpoints Dhruv wrote.

- [x] Nested job-password redaction test (Step 2b, tests/unit/test_logging.py)
- [x] Money parsing + formatting edge-case tests (Step 2d)
- [x] 3 merchants + `test_merchant_categories_exist` (Step 3)
- [x] App version field in /health (Step 4) — done together; `app.__version__` + pyproject match test
- [x] Dependency caching test (Step 5, tests/unit/test_dependency_injection.py)
- [x] Expired access token test (Phase 1 Step 1, tests/unit/test_security.py) — plus a
  just-before-expiry boundary test
- [x] `test_refresh_after_logout_is_401` (Step 2, tests/integration/test_auth.py)
- [x] `test_register_and_login_have_separate_counters` (Step 3, tests/integration/test_rate_limit.py)

- [x] `PATCH /accounts/{id}` service + route + explicit-null validator (Step 4; finished
  together, tests/integration/test_accounts.py)
- [x] ATM / NACH-EMI / salary narrations in tests/unit/test_normalize.py (Phase 1 Step 5;
  finished together). Three keys are "wrong on purpose" and open for Step 6: ATM → `CASH`,
  NACH/EMI → `EMI` (not the lender), NEFT salary → `HDFC` (IFSC bank code, not the employer)

- [x] Signed-amount-column branch in `GenericCsvParser._parse_amount` (Step 6) — done and
  reviewed; the three `test_signed_amount_column_*` tests now run (no skips left)
- [x] Chunk-boundary import test (Step 7) — completed by Claude on 2026-10-12 instead:
  `many_rows()` + `test_a_statement_larger_than_one_chunk_is_fully_imported` and
  `test_a_large_statement_reimports_as_all_duplicates` in tests/integration/test_uploads.py.
  Verified against a deliberately broken slice (`values[start:SIZE]`), which both tests
  catch; the row count is read from `_INSERT_CHUNK_SIZE` so tuning it keeps them honest

- [x] Step 7 code review (`/code-review high` on PR #9, 3 findings) — all 3 fixed:
  1. **Rule 3 leak.** Only `IntegrityError` was caught around the chunked INSERT, so any
     other DB error reached the 500 handler, whose logged traceback included SQLAlchemy's
     `[parameters: ...]` block — up to 500 narrations, amounts and balances. Proved it with
     a canary narration, then fixed with `hide_parameters=True` on both engines
     (`app/db/session.py`), re-ran the proof (canary gone, exception text 2533 → 1252
     chars), and pinned the flag with a test so it cannot be dropped silently
  2. **Unbounded amount.** `parse_amount_to_paise` has no upper limit, so a misread cell
     could exceed BIGINT and produce a 500 with no row number (and trigger finding 1).
     `MAX_PAISE` in `app/parsers/base.py` now rejects it as an ordinary bad row, both ends
     for balances (an overdraft is legitimately negative)
  3. **Weak test assertion.** `test_a_mapping_cannot_rescue_an_unrecognised_workbook` only
     asserted `"CSV" in message`, but both candidate messages contain "CSV", so reordering
     the two checks in `parse_statement` would not have failed it. Both that test and its
     sibling now compare against the message constants

## Known issues
- `GET /insights/categories` over a WHOLE YEAR stops using the date bounds: at ~45% of a
  user's rows the planner reads everything for that user through a `user_id`-prefixed index
  and filters the dates from the heap (20 400 rows scanned, 11 220 discarded, 8.75 ms on
  40 800 rows). A reasonable choice at that selectivity and fine at personal-finance size,
  so nothing was changed — but this is where the endpoint hurts first. Measured in ADR 010
- The `q=` search does NOT use the trigram GIN index on `normalized_description`, although
  Step 7 added the index for it. Measured in the Step 8 review with
  `EXPLAIN (ANALYZE, BUFFERS)` on 20 000 rows: the ILIKE pattern travels as a BOUND
  parameter, and `gin_trgm_ops` can only extract trigrams from a pattern visible at plan
  time, so a search matching few or no rows is a Seq Scan over `transactions` (19 999 rows
  discarded, ~7 ms) while a common one is answered from the keyset index (~0.2 ms). Fine at
  personal-finance size. If search gets slow the fix is a different OPERATOR
  (`word_similarity` / `<%`, which does use the index with a parameter) or a tsvector
  column. Until that is decided the index only costs write time — decide in Phase 2 whether
  to switch the search or drop it
- Transactions on the SAME DAY come back in random order: the sort key is
  `(txn_date DESC, id DESC)` and ids are UUIDv4, so `balance_after_paise` looks
  non-monotonic when comparing a day against the paper statement. Nothing stores the
  statement's own row order. `RawRow.row_number` exists in the parser, but storing it needs
  migration 0006 AND two decisions first: rows of one day can come from two different
  statements (indexes from different files are not comparable) and a newest-first statement
  reverses the meaning of the index, so the importer would have to normalize the sequence
  to chronological. The cursor would also have to carry the third key component (ADR 009).
  Planned with Phase 2 balance reconciliation (SPEC §6.4), which needs the same ordering
- Flaky once (2026-10-12): the first full `pytest` run after `docker compose up -d` failed
  with 14 failures + 10 fixture errors, all in test_accounts.py; three later runs (and
  test_accounts.py alone) are clean. Not reproduced and the output was not kept — suspect
  the containers still warming up. If it happens again, save the failure output: a 429 in
  there would point at the rate-limit reset racing a freshly started Redis
- Uploads: Starlette buffers a multipart body over ~1 MB into a temporary file before the
  route runs, so an oversized upload briefly touches the disk before the 413 (the temp file
  is deleted when the request ends). Rejecting it earlier needs a Content-Length check in
  middleware; the 10/hour/user upload limit bounds the damage meanwhile
- Postgres' own error text (`DETAIL: Failing row contains (...)`) can still carry row
  values for a NOT NULL or CHECK violation, and that part is not affected by
  `hide_parameters`. Every such violation reachable today is unreachable in practice (the
  importer fills every NOT NULL column, and `amount_paise > 0` is enforced before the
  INSERT), so this is a watch item rather than an open hole: if a future table lets a
  constraint fire on user data, catch the error in the service instead of letting it reach
  the 500 handler. Revisit in the Phase 5 security review
- slowapi 0.1.10 crashes with Redis down; worked around in `RateLimitMiddleware`
  (regression test in tests/integration/test_rate_limit.py). Recheck on upgrade
- Production behind a proxy needs uvicorn `--proxy-headers --forwarded-allow-ips`,
  or every client shares the proxy's IP for rate limits (deployment phase)
- Windows: use `127.0.0.1`, not `localhost`, in DB/Redis URLs (IPv6 `::1` hangs with Docker);
  engine has a 10 s connect timeout so this fails loudly instead of hanging
- Run tests from `backend/` with the venv Python, not Anaconda's `python`
- Unhandled-exception 500 responses have no security headers and no X-Request-ID
  (Starlette's ServerErrorMiddleware builds them outside our middleware). Body is
  generic, so low risk; decide whether to fix before Phase 5 security review
- The 95 integration tests need `docker compose up -d` (Postgres + Redis) first; without it
  they ERROR at fixture setup with a SQLAlchemy connection error, which is an environment
  problem and not a code failure. Use `pytest -m "not integration"` to skip them deliberately
