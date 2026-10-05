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

## In progress
- (nothing yet)

## Next up
- [ ] Step 7: Uploads + import service (migration 0005 — 0004 is the accounts unique key,
  idempotent inserts)
- [ ] Step 8: Transactions list + manual categorization, GET /categories
- [ ] Step 9: Insights — monthly summary + category breakdown
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
  (e.g. Axis + Kotak, both GENERIC savings ...1234) still get 409. Open: decide when
  generic CSV upload exists (Step 6/7) — add banks, or key GENERIC accounts differently
- Accounts → changing `account_type` after transactions exist (savings <-> credit_card)
  would change how existing debits/credits are read. Open: block or handle it in Step 7
- Accounts → decided (confirmed by Dhruv 2026-10-01): `bank_code` is a VARCHAR+CHECK enum (HDFC/SBI/ICICI/GENERIC; new
  bank = migration); nickname required (1–50); only nickname/account_type are editable
- Rate limiting → decided: key = user id from a valid JWT, else client IP (never
  X-Forwarded-For); general limit counted across the whole API; /health exempt;
  Redis down = fail open (ADR 007)

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
  headerless CSV is not supported in Phase 1. Open: add index-based mapping if a real file
  needs it (decide in Step 7, where the Pydantic `ColumnMapping` schema is written)
- Generic CSV → decided: `ColumnMapping` is a frozen dataclass in the parsers package
  (pure layer) and Step 7 adds the Pydantic request schema that builds it; validation
  raises `ValueError` there and the schema mirrors the same rules for a 422
- Test fixtures → decided: the ICICI `.xls` fixture is GENERATED by
  `tests/fixtures/icici_xls.py` (dev dependency `xlwt`) into `tmp_path`, not committed as a
  binary; no real statement is ever committed (rule 8)

## My TODO(dhruv) tasks
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

## Known issues
- Engines don't set `hide_parameters=True`: an unexpected DB error logs a traceback whose
  text includes SQL parameters (and Postgres error detail can include row values).
  Rule 3 risk; decide on a fix (hide_parameters + scrub exception text) before Phase 5
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
