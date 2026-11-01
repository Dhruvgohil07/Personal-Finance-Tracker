# Progress

## Current phase
Phase 2 — Real-world imports (plan: docs/plans/phase-2.md, approved 2026-10-14).
Phase 1 — Backend MVP is COMPLETE (plan: docs/plans/phase-1.md, acceptance:
docs/phase-1-acceptance.md).

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

- [x] Step 9 code review (`/code-review high` on PR #11, 1 finding) — fixed:
  **`Month.parse` accepted a trailing newline.** `MONTH_PATTERN` is read by TWO regex
  engines and they anchor differently: Pydantic compiles it with the Rust crate and applies
  it as a SEARCH (so the `^...$` anchors are required), while Python's `re` treats `$` as
  "end of string OR just before a trailing newline" (so `match` accepted `"2026-08\n"`).
  `GET /insights/summary` was never affected — Pydantic answered 422 — but `Month.parse` is
  a pure public function documented to validate, and `/insights/trend`,
  `/budgets/status?month=` and the recurring cadences are all meant to reuse it. Fixed with
  `fullmatch` (kept `$`: Rust spells end-of-string `\z`, Python `\Z`, so `$` is the only
  anchor both understand). Added `test_the_schema_and_month_parse_agree_on_what_is_malformed`,
  which runs one list of malformed values through BOTH validators so they cannot diverge
  again; verified both new tests fail against the old `match`. 848 tests pass.
  Three things the review checked and found correct, so they need no re-litigating: the
  `ORDER BY amount_paise DESC` alias is unambiguous (Postgres resolves ORDER BY against
  output names first; the plan confirms `Sort Key: (sum(t.amount_paise))`), the
  `totals.get(month) or ...` idiom is safe (a dataclass has no `__bool__`/`__len__`, so a
  zero-valued `PeriodTotals` is truthy), and the unscoped `LEFT JOIN categories` cannot
  leak another user's category because `_set_category` is the only writer of
  `transactions.category_id` and it goes through `categories_service.get_category`

- [x] Step 10 (part A): README Phase 1 walkthrough + `docs/phase-1-acceptance.md`.
  The README now documents the whole flow endpoint by endpoint (register → login →
  account → upload → list/page → categorize → both insights), with the deliberate Phase 1
  gaps listed so the limits are not mistaken for bugs; project layout and design
  principles brought up to date. Verified the LIVE server end to end (real uvicorn on
  :8111 with `DATABASE_URL` pointed at `kharcha_test`, synthetic ICICI `.xls`):
  **73/73 checks** — the flow above plus a wrong-password 401, cursor paging with no
  repeats, the self-transfer exclusion, cross-user isolation, four validation 422s and the
  security headers. Also re-checked rule 3 against real server logs — no narration,
  amount, passphrase or token in any of the 46 lines. Two README examples were wrong and
  are fixed: the `column_mapping` field names (`date_column`, not `date`) and its
  header-label-only nature. Dev database untouched by the run

- [x] Step 10 code review (`/code-review high` on PR #12, 2 findings) — both fixed. The
  diff was docs-only, so the review verified the factual claims against the code instead,
  and two of mine were wrong:
  1. **"100% of every module added in Phase 1" was false.** A coverage run shows nine
     Phase 1 modules below 100% (lowest 93%: `models/user.py`,
     `models/refresh_token.py`). The 100% figure is true only of the five modules Step 9
     added. `docs/phase-1-acceptance.md` now carries the real per-module table
  2. **The live run's value over `TestClient` was overstated.** I claimed it uniquely
     exercised ASGI startup, middleware order, multipart parsing, the Redis rate limiter
     and the OpenAPI schema. Checking each one: **all five are already covered** by the
     committed tests (`test_app_starts_and_shuts_down_cleanly` uses
     `with TestClient(...)` so lifespan does run; `test_429_passes_through_the_other_middleware`
     and `test_cors_preflight_also_gets_security_headers` assert middleware order;
     `test_rate_limit.py` drives the real Redis limiter; `test_uploads.py` posts real
     multipart; `test_health_is_listed_in_openapi_docs` fetches `/openapi.json`). The only
     thing exclusive to the live run is uvicorn's HTTP/1.1 parsing over a real socket. Part
     A is therefore an INDEPENDENT confirmation, not extra coverage — its worth is that it
     was written separately and agreed. Corrected in both places the claim appeared.
     Note: my first attempt at this fix was also wrong (it claimed lifespan was uncovered),
     which is what checking `tests/unit/test_app.py` instead of asserting caught
  Everything else in the walkthrough checked out against the code: the 5/min/IP and
  10/hour/user limits, the 400 for a PDF and for an account-number mismatch, the 409 on a
  re-upload, `expires_in: 900`, 25 categories, default limit 50, the refresh cookie's
  `path=/api/v1/auth`, every layout path, every doc link and the heading anchor

- [x] Step 10 (part B): **PASSED on the real ICICI statement** (2026-08, account ...5606,
  175 lines) — full results in `docs/phase-1-acceptance.md` §Outcome. 175 parsed / 175
  inserted / 0 duplicate; identical re-upload 409; `/insights/summary` spend ₹99,036.54 and
  income ₹74,414.50 EQUAL the file's own totals; the breakdown adds up; manual
  categorization and the self-transfer rule behave as specified (20/20 endpoint checks).
  The decisive check was the **balance chain**: `balance[n] − balance[n−1] == ±amount[n]`
  across all 174 links in file order, so every amount and direction agrees with the bank's
  running balance — a column the importer stores but never derives anything from. Opening
  ₹1,11,541.33 and closing ₹86,919.29 confirmed by Dhruv against netbanking, which is the
  one thing no check inside the file can do (the chain survives rows dropped from the start
  or end of the table). NOT covered: the overlapping-statement case (only one export
  exists) and dates (ascending across all 175 rows with 30 distinct dates, but a misread
  date within the month would survive every check here)

## Completed — Phase 1 is done
All ten steps built, reviewed and merged; the acceptance run passed on the real statement.

- [x] Coverage cleanup (post-Phase 1, 2026-10-13): **100% of statements AND branches**,
  873 tests (was 99% / 848). 25 tests closed the 20 uncovered lines, and every one of them
  tests the behaviour the line exists for rather than just touching it: the five model
  `__repr__`s now assert that no email, nickname, account number or token hash can reach a
  log (rule 3, new `tests/unit/test_model_reprs.py`); `_retry_after_seconds` answers None
  with Redis down and never rounds to "Retry-After: 0"; an ICICI export trimmed to the five
  REQUIRED header labels still imports (no "S No."/"Value Date" → `value_date` None);
  an `#N/A` cell and a sheetless workbook are handled; login re-hashes a password stored
  with weaker argon2 parameters; logout with an unknown token revokes NOTHING; a NOT NULL
  or FK violation on register / PATCH /accounts / upload stays a 500 instead of being
  reported as a 409 (three services, three real Postgres errors); and `python -m app.db.seed`
  is run end to end. The only exclusion is the `if __name__ == "__main__":` guard, declared
  once in `[tool.coverage.report]` (`backend/pyproject.toml`) — no `# pragma: no cover`
  anywhere. Two of the new tests reach past the public function (a stubbed xlrd workbook,
  a stubbed ownership lookup) and each says in its docstring why no real fixture can
  produce the case

## Completed — Phase 2
- [x] Step 0: Phase 2 plan approved and saved — docs/plans/phase-2.md (11 steps; P2P
  detection pulled forward from Phase 4; password PDFs unlocked in the request with
  `pypdf`; `q=` search/trigram index deferred to Phase 3)
- [x] Step 1: Upload lifecycle schema + file encryption — migration 0006 (`upload_blobs`;
  dedupe level 1 is now the partial unique index
  `uq_statement_uploads_user_id_file_sha256_not_failed`, `WHERE status <> 'failed'`),
  `app/core/crypto.py` (AES-256-GCM, upload id as associated data, 32-byte key enforced),
  `_already_uploaded` ignores failed rows, `fail_under = 100` (909 tests, 100%);
  ADR 011 (supersedes 008) + ADR 012. Blobs are not written yet — Step 2 does that.
  Review of the PR (5 findings, all fixed): migration 0007 adds
  `ix_statement_uploads_user_id_created_at` (the partial index had left the user CASCADE
  and the Step 2 list on seq scans); `NOT_FAILED_SQL` is one literal-SQL predicate shared
  by the index and the duplicate check (a bound 'failed' defeats the partial index in a
  prepared/generic plan — measured); `decrypt_file` maps a bad nonce to
  `FileDecryptionError`; stale constraint references fixed. Later Phase 2 migrations shift
  by one (plan change log)
- [x] Step 2: RQ worker + asynchronous uploads — `backend/Dockerfile` + compose `worker`
  service (`python -m app.workers.worker`, so log redaction is configured), `app/workers/`
  (`queue.py`, `jobs.py`, `worker.py`), `imports.py` split into `create_upload` (API: size,
  account, sha256, signature → encrypted blob + `pending` row → enqueue `(upload_id,)`) and
  `process_upload` (worker: claim → decrypt → parse → account check → insert → `completed`),
  `POST /uploads` → **202**, new `GET /uploads`, `GET /uploads/{id}`, `DELETE /uploads/{id}`
  (409 while `processing`); migration 0008 (`statement_uploads.column_mapping` jsonb);
  503 + `failed` when Redis is down; ADR 013. 963 tests, 100% coverage
- [x] Step 2 code review (`/code-review high` on PR #17, 8 findings) — all 8 fixed:
  1. a job whose process died or was stopped now also ends `failed` (`on_stopped` +
     the worker's `work_horse_killed_handler`; RQ skips `on_failure` for both). The
     parent-process hooks use an unpooled engine
  2. DELETE uses `SKIP LOCKED`: 409 at once while an import runs, instead of waiting
     for it and then deleting it
  3. DELETE refuses (409) an upload a LATER overlapping upload relies on, since the
     later one's skipped duplicates belong to it
  4. the import locks user → account → upload (`FOR KEY SHARE` first), the order
     DELETE /accounts and DELETE /me use: no deadlock (tested with a concurrent DELETE)
  5. an enqueue error after the job got through answers 202, not 503
  6. a duplicate of an unfinished upload says "being imported" and names it; one
     unfinished for over 1 h (`STALE_UPLOAD_AFTER`) is failed on the spot
  7. the worker reads by the stored file type (`read_statement_as`,
     `READABLE_FILE_TYPES`), not a made-up filename
  8. `GET /uploads/{id}` has its own limit (120/min) and sends `Retry-After: 2` while
     unfinished (ADR 007 amendment)
  978 tests, 100% coverage. ADR 013 amended

- [x] Step 3: Cleanup job + scheduler — `app/services/cleanup.py` (`run_cleanup`: three
  set-based rules in one transaction: `pending`/`processing` uploads > 1 h → failed + file
  deleted; stored files > 24 h deleted and a still-waiting upload (e.g. `needs_password`)
  failed as expired; refresh-token families deleted only when EVERY token expired),
  `jobs.cleanup()`, `queue.enqueue_cleanup` (ttl 1 h, so a stopped worker never builds a
  backlog), `app/workers/scheduler.py` (APScheduler `BlockingScheduler`, only enqueues, runs
  once at start then hourly), compose `scheduler` service; ADR 005 amendment. 999 tests,
  100% coverage. Live: scheduler enqueued at startup, worker ran it in 55 ms
- [x] Step 3 code review (`/code-review high` on PR #18, 9 findings) — all 9 fixed:
  1. a re-upload re-reads whether a live upload is left after applying the stale rule, so
     an upload the cleanup failed a moment earlier frees the file (was a false 409)
  2. the scheduler handles SIGTERM (PID 1 ignores it otherwise) + `init: true` in compose:
     `docker compose stop scheduler` 0.6 s instead of 10 s + SIGKILL
  3. each cleanup rule commits on its own: short row locks
  4. "stale" also requires that no job of the upload waits in Redis (fixed job id
     `process_statement_{upload_id}`, `statement_job_waiting_check`; Redis down = waiting)
  5. dead token family = newest token expired over 1 h ago (`DEAD_FAMILY_GRACE`)
  6. redaction lets integer counts through (`_count`/`_deleted`/`_failed` suffix AND int),
     so `refresh_tokens_deleted` is logged under its real name
  7. token DELETE reads only expired rows (migration 0009 index) + `NOT EXISTS` live sibling
  8-9. one `fail_stale_uploads` + `fail_uploads_where` shared by cleanup and re-upload
  1015 tests, 100% coverage. ADR 005 and ADR 013 amended

- [x] Step 4: Statement order + balance reconciliation — `app/parsers/reconcile.py` (pure:
  `to_chronological` keeps or REVERSES rows, never sorts; `reconcile` checks each row
  against the balance printed before it + opening/closing when known), migration 0010
  (`transactions.statement_seq` back-filled + UNIQUE per upload, keyset index
  `(user_id, txn_date DESC, statement_seq DESC, id DESC)`,
  `statement_uploads.reconciliation_issues`), worker sets `needs_review` when issues exist
  (data kept), `GET /uploads/{id}` returns them (null = not checked, [] = clean), 3-part
  cursor (old 2-part cursors → 422); ADR 014. 1047 tests, 100% coverage. **Real ICICI
  statement: 175 rows, 0 issues**
- [x] Step 4 code review (`/code-review high` on PR #19, 7 findings) — 6 fixed, 1 skipped:
  1+4. parsers no longer derive opening/closing (ICICI swapped them for a one-day
       newest-first file); they mean "printed on the statement" only
  2. rows without a balance are carried forward and checked at the next printed balance
     (no false `needs_review` for once-a-day balances; `missing_balance` kind removed)
  3. each day is ordered by its balance chain (days oldest-first, transactions newest-first)
  5. `RawRow.signed_paise` replaces two copies of the sign logic
  6. SKIPPED: migration 0010 rewrites every row + rebuilds the index in one transaction;
     it is committed (rule 7), no production DB exists yet; the pattern for large tables
     is recorded in ADR 014
  7. the reconcile loop's `first` flag is gone (`_balance_checks` generator)

- [x] Step 5: Channel, VPA and merchant key from real narrations — golden test FIRST
  (`test_golden_fingerprints.py`: 18 rows, literal normalized strings + fingerprints,
  verified to fail on a mutated normalizer); real narrations profiled by shape and
  4 rules confirmed with Dhruv; new pure `app/parsers/narration.py` (ICICI UPI and
  IMPS layouts read by position, first-word channels, SPEC VPA regex as fallback,
  `merchant_key_candidate` moved here and now skips VPA chunks); `Channel` moved to
  `app/parsers/base.py`; `NormalizedRow.channel` + `counterparty_vpa`; migration 0011
  (`transactions.merchant_key` + `(user_id, merchant_key)` index); the importer stores
  all three; ADR 015. 1125 tests, 100% coverage. **Real statement: fingerprints
  identical to before on all 175 rows**; 172 UPI / 2 IMPS / 1 ATM, 174/175 rows with
  a merchant key
- [x] Step 5 code review (`/code-review high` on PR #20, 8 findings) — all 8 fixed:
  1. whole-name keys matched the seeded dictionary on 2 real rows instead of 16 (ICICI
     cuts "Swiggy Limited" to `SWIGGYLIM`). New pure `dictionary_key`: exact match, else
     the longest seeded key of 5+ characters the key starts with → 16 again. **Step 6
     must use it** for the dictionary lookup
  2. a real counterparty name had been committed in the `name_key` docstring and
     ADR 015 (and pasted into the PR description) → replaced with an invented example.
     The earlier pushed commits still contain it (not rewritable)
  3. an all-digit VPA that is not a phone number is an account number → NULL
     (CLAUDE.md §7.3); 5 real rows. Now 165/172 UPI rows have a VPA
  4. noise words are derived from the channel table (`UPL` the one exception)
  5. both ICICI layouts are anchored at both ends, so a `/` in the remark no longer
     changes the key
  6. the VPA regex requires the handle to end there, so an email is not a VPA
  7. company filler is dropped only as a suffix (`INDIA GATE` keeps its name)
  8. `detect_channel` matches the first word instead of splitting the whole narration

- [x] Step 6: Categorizer chain — new pure package `app/categorizer/` (`base.py`: `TxnFacts`,
  `Decision`, `Handler`, `categorize`, `CategorySource` moved here; `handlers.py`: user
  rules, merchant dictionary via `dictionary_key`, keywords, P2P, `build_chain`; purity
  test), `app/services/categorization.py` (`categorize_upload`, same DB transaction as
  the import, only `none` rows, also sets `merchant_id`), `rows_categorized` in the
  import log, insights null bucket renamed "Not categorized yet"; ADR 016. No migration.
  1220 tests, 100% coverage. **Real statement: 89/175 = 50.9% categorized without an
  LLM** (49 Transfers to People, 19 Other Income, 14 dictionary, 6 Refunds, 1 Cash)
- [x] Step 6 code review (`/code-review high` on PR #21, 8 findings) — all 8 fixed:
  1. bank-wording keywords (charges, salary, interest) no longer read UPI/IMPS
     narrations, whose remark the payer types: a person paid with the remark "charges"
     had become Fees & Charges (reproduced). BHIM cashback now requires NPCI as payer
  2. a merchant's credit with an income keyword is Salary/Interest, not a Refund (big
     employers are seeded merchants)
  3. a rule's category id is written only if it is a system category or the user's own
     (ADR 010's unscoped join relies on it); refused rows are logged
  4. ADR 010 amended for the "Not categorized yet" rename
  5. the P2P business-word check reads the counterparty's NAME field
     (`counterparty_name`), not the remark
  6. missing slugs and refused ids are logged once per upload with a count
  7. `dictionary_key` is looked up once per row and carried in `TxnFacts`
  8. one word splitter, `key_words`, shared by narration.py and the categorizer
  1236 tests, 100% coverage; real statement unchanged at 89/175 (50.9%)
- [x] Step 7: User rules + `apply_to_similar` — migration 0012 (`category_rules`),
  `app/models/category_rule.py`, `schemas/rule.py`, `services/rules.py`,
  `api/v1/rules.py` (`GET/POST/PATCH/DELETE /rules`), `app/categorizer/learning.py`
  (`learned_rule_key`: a person's VPA, else the merchant key, else 400
  `NOTHING_TO_MATCH`), `normalize_rule_pattern`. Every rule change re-runs the chain
  over all of the user's non-manual rows (job `recategorize_user(user_id)`, run in the
  request if Redis is down). A per-user advisory lock serializes import and re-run
  categorization. ADR 017. No new dependency. 1301 tests, 100% coverage
- [x] Step 7 code review (`/code-review high` on PR 22, 9 findings) — all 9 addressed
  (ADR 017 amendment):
  1. a contains pattern holding a 6+ digit number is refused ("upi 9876543210" had
     become "UPI", matching every UPI row)
  2. priority ties go to `updated_at`, so a re-taught rule beats a newer one
  3. VPA rules match VPAs ICICI cut at 14 characters, both ways round
  4. `GET /transactions` returns `merchant_key`, the value a merchant rule matches
  5. IMPS people stay learned by cut name (documented, decided with Dhruv)
  6. with Redis down, the rule change and the re-run are one transaction
  7. an update that changes nothing skips the commit and the re-run
  8. one rule order, `rules_in_order`, for the categorizer and `GET /rules`
  9. one self-transfer rule, `CategoryKind.marks_self_transfer`

  1320 tests, 100% coverage
- [x] Step 8: Self-transfer detection — pure matcher `app/categorizer/self_transfer.py`
  (one-to-one, nearest gap first, a row ambiguous at its nearest gap stays unmatched),
  `_detect_self_transfers` at the end of BOTH categorization passes (import and rule
  re-run), so rule changes keep the pairs. Pairs become Self Transfer / `heuristic` /
  0.80 / `is_self_transfer`. User rules and manual rows are never paired. Re-filing a
  half by hand re-runs the user's rows. No migration, no new job. ADR 018. Verified on
  synthetic two- and three-account statements only (one real account so far).
  1350 tests, 100% coverage
- [x] Step 8 code review (`/code-review high` on PR 23, 7 findings) — all 7 addressed
  (ADR 018 amendment):
  1. manual Self Transfer rows anchor a pair (confirming one half used to strip the
     other)
  2. only uncategorized or `heuristic` rows pair: rent and an equal salary no longer
     erase each other
  3. deleting an upload or account that breaks a pair re-runs the user's rows
  4. lock order fixed: a category change takes the per-user lock before the row
  5. clearing a detected half may re-pair it (documented, decided with Dhruv)
  6. the import pass reads only the new rows' amounts
  7. `commit_and_recategorize` moved to the categorization service

  1360 tests, 100% coverage
- [x] Step 9: ICICI PDF parser — `read_pdf` (pdfplumber) passes words with positions
  (`ParserInput.pdf_words`, `PdfWord`), and the pure `IciciPdfParser` rebuilds the rows:
  - a row starts one line above its dated line; the footer is cut at the first gap;
  - direction comes from the amount's column;
  - the name line is dropped when it repeats the UPI name;
  - wrapped lines are joined by a measured rule (a one-word line within 25 pt of the
    edge = a mid-token split).

  `account_last4` is shared with the `.xls` parser, and PDFs are accepted at upload.
  New dev dependency `fpdf2` builds synthetic PDFs (`tests/fixtures/icici_pdf.py`).
  ADR 019. **Real PDF: 178/178 rows, balances reconcile (0 issues), last 4 matches the
  `.xls`, 177/178 narrations rebuild the `.xls` layout exactly.** The same rows as
  `.xls` and as PDF give 100% duplicates (synthetic, both orders).
  1399 tests, 100% coverage
- [x] Step 9 code review (`/code-review high` on PR 24, 8 findings) — all 8 addressed
  (ADR 019 amendment):
  1. the account and period come only from page 1's title block
  2. any failure while reading a PDF is one plain `ParseError`
  3. remarks carried over to the next page join the previous row (decided with
     Dhruv)
  4. the name line matches a name field ending in a space
  5. file types are dispatched by name
  6. the header's third line is header
  7. a local real-file cross-check test, `tests/unit/test_real_samples.py` (decided
     with Dhruv)
  8. words split by page once; row owners found by binary search

  The real PDF parses identically after each fix. 1409 tests (+1 skipped: the
  real-samples check), 100% coverage
- [x] Step 10: Password-protected PDFs. New dependency `pypdf`. `unlock_pdf`
  (`app/parsers/pdf_unlock.py`) takes the lock off in the API request; the unlocked
  copy is stored app-encrypted, so the worker never sees a password. The flows:
  - with the password: 202;
  - without it: `needs_password`, 409 `PASSWORD_REQUIRED` + `upload_id`;
  - then `POST /uploads/{id}/password` (5/min/user): 202;
  - a wrong password: 400 `INCORRECT_PASSWORD`.

  The password is a `SecretStr` from the route to the unlock. One test searches
  responses, raw logs, every table, the stored file and all of Redis for the
  password; each check was shown to catch a planted leak. The unlocked copy reads
  identically (all 5 encryption methods, fpdf2-encrypted, and locally the real PDF).
  ADR 020. No migration. Live check against the Docker worker: 409 → 400 → 202 →
  `completed`, no password in any log.
  1464 tests (+1 skipped), 100% coverage
- [x] Step 10 code review (`/code-review high` on PR 25, 10 findings) — all 10 addressed
  (ADR 020 amendment):
  1. the unlocked copy is size-checked (413). The request's pypdf cost was measured
     (0.27 s for 9 MB; pypdf's own budgets refuse object bombs) and no timeout added
  2. no row lock or connection held while pypdf runs; the store step re-checks
  3. a PDF pypdf cannot decrypt goes to the worker unchanged
  4. a missing stored file says "no longer available"
  5. the race loser still gets `PASSWORD_REQUIRED` + the upload id
  6. `select_owned_upload` shared
  7. `process_upload` uses `_decrypted`
  8. test helpers shared
  9. the PDF is parsed once
  10. the message constant moved to `readers.py`

  The five new behaviour tests were checked to fail on the pre-review code.

## In progress
- (nothing)

## Next up
- **Real cross-check of Step 9:** put an ICICI `.xls` of the PDF's exact period in
  `samples/`, then run `uv run pytest tests/unit/test_real_samples.py` (counts only;
  skipped until such a pair exists). The two current samples share no day
- Step 11 (acceptance): its self-transfer check needs a second real account's statement

## Open decisions / questions
- Git workflow → decided: `main` protected (ruleset `protect-main`); every change via a
  feature branch + PR, green `backend` check required, rebase-merge only (CLAUDE.md §18)
- RQ worker on Windows → decided: run as a Docker service (added in Phase 2 Step 2)
- Step 2 → decided (ADR 013): the request does only the cheap checks (size, ownership,
  sha256, file signature); parse errors and the wrong-account check are found by the worker
  and reported as `failed` + `error_message`, no longer as a 400. PDFs are refused in the
  request (400) until Step 9
- Step 2 → decided with Dhruv: a CSV's `column_mapping` is stored on the upload row
  (migration 0008), not in the job args (SPEC §11: upload id only) or on `upload_blobs`
- Step 2 → found in RQ 2.12's source: `on_failure` runs after EVERY failed attempt, before
  the retry decision, so the callback checks `job.should_retry` and only fails the upload
  when no retry is coming
- Step 2 review → decided: deleting an earlier upload that a later overlapping one
  relies on is REFUSED (delete the later one first) rather than allowed. Nothing
  records which upload a skipped duplicate matched, so the check errs on refusing
- Step 2 review → decided: `STALE_UPLOAD_AFTER = 1 h` (the Step 3 cleanup reuses it)
- Step 3 → decided (ADR 005 amendment): the plan's "delete expired refresh tokens" taken
  literally would switch off reuse detection for a thief who outlasts the stolen token, so
  a token FAMILY is deleted only when every token in it has expired. A test proves the naive
  rule would let the thief's token keep working
- Step 3 → found in the live run: log keys are redacted by substring, so a count named
  `refresh_tokens_deleted` came out as `[REDACTED]` ("token"). Fixed at the root in the
  review: the redaction lets INTEGER values of keys ending in `_count`/`_deleted`/`_failed`
  through (a string under such a name is still redacted). A test runs the cleanup's log
  line through the real processor (`capture_logs` skips processors)
- Step 4 → decided: `reconciliation_issues` is NULL when nothing could be checked (no
  balance column) and [] when checked and clean; at most 100 stored, full count in
  `reconciliation_errors`. Issues carry the FILE row number and a kind, never amounts
- Step 4 → decided: issue kinds `balance_mismatch`, `opening_balance_mismatch`,
  `closing_balance_mismatch` (`missing_balance` was dropped in the review: a blank
  balance is carried forward, not an error)
- Step 3 → note: the stale-upload rule scans `statement_uploads` without an index on
  `status` (hourly; fine at personal scale). A partial index `WHERE status IN
  ('pending','processing')` is the fix if it ever shows up
- Step 2 → note: tests run jobs on `Queue(is_async=False)` named `kharcha-tests`, so RQ's
  retries are not exercised end to end; the callback's branches are tested directly and
  the retry options are read back from a job stored in Redis
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
- Merchant key format → decided in Phase 2 Step 5 (ADR 015, with Dhruv): for ICICI's
  UPI/IMPS layouts the WHOLE name field joined (`MOON CAFE` → `MOONCAFE`); elsewhere
  the first name-like word, skipping VPA chunks (no more "YBL"); ATM rows have none.
  For P2P the key is a person's cut name: Step 6 must keep it out of the LLM and the
  global cache (§8.3)
- VPA → decided (ADR 015, with Dhruv): stored AS PRINTED, lowercased, even when ICICI
  cut it at 14 characters (87 of 171 real UPI rows lost the `@handle`). Open for Step 9:
  if the PDF prints full VPAs, a VPA rule learned from an `.xls` row won't match it
- Channels → decided (ADR 015): first word only; `UPI`/`UPL`/`MMT`/`NFS` from the real
  file, `IMPS`/`NEFT`/`RTGS`/`ACH`/`NACH`/`ATM`/`ATW`/`POS` from SPEC §6.5 (not seen in
  real data yet). Interest and charges stay `other` until a real ICICI sample shows
  their wording
- Categorizer → decided (ADR 016, with Dhruv): P2P credits = Other Income; a merchant
  credit = Refunds; SPEC §8 keyword examples built and marked unseen. Step 9's
  `LEFT JOIN categories` invariant still holds: the categorizer only writes SYSTEM
  category ids (`user_id IS NULL`); Step 7 must check a rule's category belongs to its
  user before the rule handler can write it
- Merchant seed → decided (ADR 015, with Dhruv): NOT extended from the real file. Its
  businesses are local shops/campus vendors; user rules (Step 7) learn them per user
- User rules → decided (ADR 017, with Dhruv):
  - every rule change re-runs the chain over ALL of the user's non-manual rows
    (`recategorize_user`, not SPEC's `recategorize_by_rule`);
  - `apply_to_similar` on an existing rule updates it (the latest correction wins);
  - with Redis down, the re-run happens in the request, in ONE transaction with the
    rule change (review);
  - ties in priority go to the rule created or changed most recently (review);
  - VPA rules allow for ICICI's 14-character cut (review);
  - an IMPS person is learned by their cut name, a known collision risk (review);
  - nothing to match on is a 400, not a 422;
  - PATCH /rules changes only the category and priority.

  The Step 6 check that a rule's category belongs to its user is now enforced in both
  places: `POST/PATCH /rules` (404) and where the id is written
- PDF parsing → decided (ADR 019, with Dhruv):
  - word positions travel in `ParserInput.pdf_words`, so the parser stays pure;
  - the name line is dropped only when it equals the UPI name field;
  - unseen PDF row kinds (NEFT, ATM) are parsed with the same rules;
  - the cross-format check runs on a same-period real `.xls` (pending).

  `uv` was installed with winget (0.13.6). It bumped `uv.lock` to `revision = 5`; CI's
  setup-uv installs the latest uv, which reads it
- Self transfers → decided (ADR 018, with Dhruv):
  - detection runs in every categorization pass, not as a `post_import_analysis` job;
  - only uncategorized or P2P-`heuristic` rows pair; merchant, keyword and user-rule
    rows never do (narrowed in the review);
  - a manual Self Transfer row is an anchor: its partner pairs to it (review);
  - re-filing a row that is a self transfer before or after re-runs the user's rows,
    and so does a delete that breaks a pair (review);
  - clearing a detected half may re-pair it; choose another category to reject a pair
    (review);
  - no pair link column.

  Open: the credit must come 0–2 days AFTER the debit; revisit if a real bank posts the
  credit side first
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

- Step 7 → decided (ADR 008, SUPERSEDED by ADR 011 in Phase 2 Step 1: failed rows are now
  recorded and the partial file-hash index skips them): a FAILED import writes no
  `statement_uploads` row in Phase 1.
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
  by `category_id` being null. Decided in the Phase 2 plan: the categorizer leaves
  "nothing matched" rows NULL, and the null bucket is renamed "Not categorized yet" (Step 6)
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
- Step 9 → note (checked in the review, no change made): `date_trunc('month', txn_date)`
  resolves to the **timestamptz** overload (the plan shows `(txn_date)::timestamp with time
  zone`), so the grouping nominally runs in the session timezone. Tested across UTC,
  Asia/Kolkata, UTC+14, UTC−12 and a DST zone with a midnight transition: the
  date → timestamptz → truncate → date round trip returns the correct 1st in every case,
  because the same zone is used for all three steps. Correct, not lucky — but
  `txn_date::timestamp` would make it timezone-independent by construction. Decide if a
  future month-grouped query (the trend endpoint) makes it worth the churn
- Step 9 → open (raised in the review, not acted on): the `LEFT JOIN categories` in the
  breakdown has no `user_id` predicate on `c`, so it relies on an invariant enforced in
  another module (only `_set_category` writes `transactions.category_id`, and it validates
  ownership). Safe today and no failing scenario exists. Phase 2's categorizer and
  `POST /categories` must preserve it; adding
  `AND (c.user_id IS NULL OR c.user_id = :user_id)` to the join would make the query
  self-defending instead
- Coverage gate in CI → DONE (Phase 2 Step 1): `fail_under = 100` in
  `[tool.coverage.report]`, so CI's `pytest --cov` goes red below 100% (statements +
  branches). Only applies when `--cov` is passed: a partial local run WITH `--cov`
  (e.g. one test file) now fails on coverage even though its tests pass — expected

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
- Unlocking a password-protected PDF runs pypdf inside the API request, with no hard
  timeout. It is bounded by the upload limit and pypdf's own parsing budgets
  (measured worst case ~2-4 s for a 10 MB synthetic file, ADR 020 amendment). If that
  ever matters, run the unlock in a process with a timeout
- **Intermittent (seen 2026-10-27, Step 7 review):**
  `test_upload_storage.py::test_a_users_uploads_are_found_through_the_user_index[upload_list]`
  failed once in four full runs. It passes alone (3/3) and in the next full run, and
  this branch does not touch `statement_uploads`. It asserts an EXPLAIN plan, so it
  likely depends on table statistics left by earlier tests. Not investigated yet: worth
  pinning the statistics (or `ANALYZE` in the test) if it shows up again, CI included
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
  column. Until that is decided the index only costs write time. Phase 2 plan: deferred to
  Phase 3, when the frontend has a search box
- **SOLVED (Phase 2 Step 4): same-day transactions came back in random order.** The
  list sorted a day's rows by a random UUID, so the balance column jumped around (151 of
  174 links looked broken in the Part B run). Now `transactions.statement_seq` stores each
  row's position in its statement, oldest first, and the list and cursor sort by
  `(txn_date, statement_seq, id)` (ADR 014). The real statement reconciles with 0 issues
- **SOLVED (2026-10-13): the "flaky integration tests" are TWO pytest runs sharing one
  test database.** Reproduced while Dhruv was running the suite in his editor and Claude
  ran it too: 8 failures + 7 errors, and the run took **454 s instead of the usual ~62 s**.
  A solo run immediately after was 871 passed in 62 s. The diagnosis is in the captured
  output: `relation "users" does not exist` mid-run, plus `409` where a `201` was expected
  (a previous test's user still present) and `401`s. Only one statement in this codebase
  drops that relation — `DROP SCHEMA public CASCADE` in the session-scoped
  `migrated_engine` fixture — so a second session was tearing the schema down underneath
  the first. pytest-xdist is not installed, so it cannot be parallel workers within one run.
  The earlier guess (2026-10-12: "containers still warming up", with a 429 from the
  rate-limit reset as the suspected cause) was **wrong**: the captured output contains only
  2 mentions of "429" and both are the rate-limit tests' own assertions.
  Seen a second time the same day, from the other side: a Claude run of test_accounts.py
  stopped making progress entirely. `pg_stat_activity` + `pg_blocking_pids()` showed why —
  the per-test teardown `TRUNCATE` needs ACCESS EXCLUSIVE on every table, so it queued
  behind the other run's connection sitting `idle in transaction`, and then blocked that
  run's next query in turn. Nothing sets `lock_timeout`, so the wait is unbounded: the
  shared database shows up either as wrong data (409s, a dropped schema) or as a run that
  simply hangs.
  **Consequence: do not run `pytest` in two places at once.** If that becomes a real
  constraint, give each run its own database (`TEST_DATABASE_URL` with a per-run suffix) or
  its own schema; a lock would just serialize them. Worth doing before CI ever runs two
  jobs against one Postgres service. A `lock_timeout` on the teardown connection would at
  least turn the hang into an immediate, readable failure
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
