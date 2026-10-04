# Phase 1 — Backend MVP (usable via /docs)

## Goal
Turn the Phase 0 skeleton into something usable through `/docs`: register
and log in, add a bank account, upload the real ICICI statement, see correct
transactions, re-upload without duplicates, categorize transactions by hand,
and see a monthly summary and category breakdown.

## Done when
- The real ICICI `.xls` statement uploads through `/docs` and the resulting
  transactions match the statement
- Re-uploading the same file is rejected; an overlapping statement inserts
  only the new rows
- Transactions can be categorized manually
- `GET /insights/summary` and `GET /insights/categories` return correct totals
- `ruff check` and `pytest` pass locally and in GitHub Actions CI

## Decisions made during planning

| Decision | Option chosen | Why |
|---|---|---|
| Primary bank | ICICI | The only real statement available (`samples/`, gitignored) |
| ICICI file format | Legacy Excel `.xls` (confirmed by the `D0 CF 11 E0` file signature) → new dependency **`xlrd`** (approved) | Users upload exactly what ICICI gives them; the `csv` module can't read the Excel binary format. ADR 004 |
| Rate limiting | Built in Phase 1 (slowapi, counters in Redis) | Login brute-force protection ships together with login |
| Extra endpoints | Only `GET /categories` (read-only) | Needed to find a `category_id` for manual edits. `/me`, upload list/delete and user categories are deferred |
| Processing | Synchronous, inside a service function | SPEC §16; the same logic moves to the RQ worker in Phase 2 |
| PDF uploads | Rejected with a clear error | PDF parsing is Phase 2 |
| Refresh token format | Opaque random token (`secrets.token_urlsafe`), stored as HMAC-SHA256 keyed with `JWT_REFRESH_SECRET` | Rotation needs a DB row anyway, so a JWT adds nothing; the HMAC key means a leaked DB alone can't confirm guessed tokens. ADR 005 |
| Swagger auth | `HTTPBearer` (paste the access token into "Authorize") | Login takes JSON, not the OAuth2 form that Swagger's password flow expects; simpler to explain |
| Schema scope | `statement_uploads` and `transactions` get their full §5 shape now (Phase 2 columns nullable or defaulted) | Same reasoning as ADR 002: final table shape early, smaller later migrations |
| Deferred to Phase 2 | Channel/VPA extraction, balance reconciliation + `needs_review`, `apply_to_similar`, `is_self_transfer` edits, upload blobs, user categories CRUD, `/me` endpoints, upload list/delete | Matches the roadmap; keeps Phase 1 focused |

## Steps

Each step ends with `ruff` + `pytest`, an explanation of every file, a "What
you should understand now" summary, a progress update, the list of files to
stage and a proposed commit. Stop after each step for review.

### Step 1 — Users, refresh tokens, security helpers
- `backend/app/models/user.py`, `backend/app/models/refresh_token.py`
- Migration `0002`: `users` (citext email, `settings` jsonb), `refresh_tokens`
  (`token_hash` unique, `family_id`, `expires_at`, `revoked_at`), and the
  deferred FK `categories.user_id → users.id ON DELETE CASCADE` (ADR 002)
- `backend/app/core/security.py` — argon2id hash/verify (+ rehash check),
  access JWT create/decode (15 min, `sub` = user id, `exp`, `type`),
  refresh token generate + hash
- `backend/tests/unit/test_security.py`; the migration test must still pass
- TODO(dhruv): unit test that an expired access token is rejected

### Step 2 — Auth endpoints + `get_current_user`
- `backend/app/schemas/auth.py` — RegisterRequest (`EmailStr`, password min
  8), LoginRequest, TokenResponse
- `backend/app/services/auth.py` — register (409 on duplicate email), login
  (generic "invalid email or password"), refresh with rotation + family reuse
  detection (a reused revoked token revokes the whole family), logout
- `backend/app/api/v1/auth.py` — `POST /auth/register|login|refresh|logout`;
  refresh cookie httpOnly, Secure, SameSite=Strict, `path=/api/v1/auth`
- `backend/app/api/deps.py` — `get_current_user` + `CurrentUser` alias
- `backend/tests/integration/test_auth.py` — full flow, rotation, reuse
  detection, wrong password, expired/garbage token → 401
- `docs/decisions/005-auth-tokens.md`

### Step 3 — Rate limiting
- `backend/app/core/rate_limit.py` — slowapi `Limiter` with Redis storage;
  key = user id from the JWT when present, else client IP; 429 handler that
  returns our `{"error": ...}` shape
- Limits: register/login 5/min/IP, uploads 10/hour/user, default 100/min/user
- Tests: the 6th login within a minute → 429 in our error shape; a fixture
  clears limiter keys so tests don't affect each other
- *Change during the step:* the general limit is one counter per client across
  the API (slowapi "application limit"), `/health` is exempt, and Redis down =
  fail open. slowapi 0.1.10 crashes (500) in that case, so a small
  `RateLimitMiddleware` subclass works around it. Recorded in ADR 007
  (004/006 stay reserved for Steps 6/5).

### Step 4 — Accounts CRUD
- `backend/app/models/account.py` — `bank_code`, `nickname`, `account_type`,
  `masked_number` (exactly 4 digits), unique `(user_id, bank_code, masked_number)`
- Migration `0003`
- `backend/app/schemas/account.py`, `backend/app/services/accounts.py`,
  `backend/app/api/v1/accounts.py` — `GET/POST/PATCH/DELETE /accounts`,
  every query filtered by `user_id`
- `backend/tests/integration/test_accounts.py` — CRUD, duplicate → 409,
  **IDOR: user B gets 404 on user A's account**
- TODO(dhruv): implement `PATCH /accounts/{id}` (service + route), then review

### Step 5 — Pure parsing core
- `backend/app/parsers/base.py` — `ParserInput` (rows as `list[list[str]]` +
  file type), `RawRow`, `ParsedStatement`, `ParseError` (with row number),
  `StatementParser` Protocol
- `backend/app/parsers/registry.py` — picks the highest `detect()` score
  above a threshold
- `backend/app/parsers/normalize.py` — uppercase, collapse whitespace, strip
  long digit sequences, basic `merchant_key` candidate (channel/VPA
  extraction is Phase 2)
- `backend/app/parsers/fingerprint.py` — §6.3 fingerprint + `occurrence_index`
- Unit tests for each (no DB or FastAPI imports; ≥ 90% coverage)
- `docs/decisions/006-dedupe-fingerprint.md`
- TODO(dhruv): normalization test cases for 3 narrations you write yourself

### Step 6 — File readers, generic CSV parser, ICICI parser
- Add `xlrd` to `backend/pyproject.toml`
- `backend/app/parsers/readers.py` — file signature + extension checks;
  CSV → rows (`csv` module, BOM-safe); XLS → rows via `xlrd`. Numeric xls
  cells come back as Python floats, so they are converted with
  `Decimal(repr(value))` rounded to 2 dp, and date cells with
  `xlrd.xldate_as_datetime`; a test proves no float error reaches paise
- `backend/app/parsers/generic_csv.py` + `ColumnMapping` schema (date column,
  description column, debit/credit **or** signed amount column, optional
  balance column, date format)
- `backend/app/parsers/icici.py` — first inspect only the header/layout of the
  real sample (column names, header row position, date format) and confirm it
  with Dhruv; no guessing. Then build a fully synthetic fixture
  `backend/tests/fixtures/icici_sample.xls`
- Tests: generic CSV with both amount styles; ICICI parser against the fixture
- `docs/decisions/004-xls-support.md`

### Step 7 — Uploads + import service
- `backend/app/models/statement_upload.py`, `backend/app/models/transaction.py`
  (full §5 shape; keyset index and trigram GIN index), migration `0005`
  (`0004` was used by the Step 4 accounts unique-key fix)
- `backend/app/services/imports.py` — validate size (`MAX_UPLOAD_MB`), type,
  account ownership → sha256 → reject re-upload (409) → parse → normalize →
  fingerprint → insert all rows in ONE DB transaction with
  `on_conflict_do_nothing` → counts (`rows_parsed/inserted/duplicate`) →
  status `completed`/`failed`. Written so the Phase 2 worker can reuse it
- `backend/app/api/v1/uploads.py` — `POST /uploads` (multipart: `file`,
  `account_id`, optional `column_mapping` JSON); response includes the counts
- Tests: upload → transactions exist; same file → 409; overlapping statement
  → only new rows; two identical same-day rows both kept; another user's
  `account_id` → 404; PDF rejected; logs contain no narrations/amounts

### Step 8 — Transactions list, manual categorization, categories
- `backend/app/services/transactions.py`, `backend/app/api/v1/transactions.py`
  - `GET /transactions` with `from, to, account_id, category_id, direction, q,
    min_amount, max_amount, cursor, limit`; keyset pagination on
    `(txn_date DESC, id DESC)` with an opaque base64 cursor; `q` uses `ILIKE`
    with bound params
  - `PATCH /transactions/{id}` — `category_id` (a system category or the
    user's own) and `notes`; sets `category_source = user_manual`
- `backend/app/api/v1/categories.py` — `GET /categories` (system + user's own)
- Tests: filters, pagination without gaps or repeats, PATCH, IDOR on PATCH
- TODO(dhruv): query validation (`min_amount <= max_amount`, `from <= to`) + its test

### Step 9 — Insights
- `backend/app/services/insights.py` — readable SQL via `text()` with bound
  params; self transfers excluded
  - `GET /insights/summary?month=YYYY-MM` — income, spend, net, % change vs
    previous month (safe when the previous month is 0)
  - `GET /insights/categories?from&to` — spend per category
- Tests with synthetic data whose totals are computed by hand
- TODO(dhruv): test that a self-transfer row is excluded from the summary

### Step 10 — End-to-end check and wrap-up
- Manual run in `/docs`: register → login → add ICICI account → upload the
  real `.xls` → compare transactions with the statement → re-upload (409) →
  categorize → check the summary
- `README.md` — Phase 1 usage walkthrough; progress and plan updated

## Change log
- **Step 5 (done):** `base.py` also defines the `FileType` (csv/xls/pdf) and `Direction`
  (debit/credit) enums, and a `NormalizedRow` type that wraps a `RawRow` with its
  normalized description and merchant key. Reasons: the pure parser layer needs both enums
  and must not import the models (Step 7's `transactions` model imports `Direction` from
  here instead), and `NormalizedRow` gives Step 7 a clean `parse → normalize → fingerprint
  → insert` pipeline instead of passing seven loose values into the fingerprint function.
  The registry deliberately does *not* fall back to `GenericCsvParser` as SPEC §6.2
  suggests: that parser needs the user's column mapping, so the fallback is Step 7's
  decision. Channel/VPA extraction stayed in Phase 2 as planned.
- **Step 6 (done):** four changes to the planned shape, all from reading the real
  sample first.
  1. The fixture is **generated, not committed**: `backend/tests/fixtures/icici_xls.py`
     writes a real `.xls` into `tmp_path` from plain-Python rows (new dev dependency
     `xlwt`), instead of a committed `icici_sample.xls`. A binary cannot be reviewed in
     a diff, and each test variant (credit row, broken date, continuation row, missing
     period) would have needed its own blob.
  2. `ParsedStatement` gained **`account_last4`** (validated as exactly 4 digits), so
     Step 7 can check the uploaded file belongs to the account the user picked. The
     statement header holds the full number and the holder's name; only four digits
     leave the parser (SPEC §7.3).
  3. The ICICI parser **joins narration continuation rows**. The real file spills a long
     narration into the next row (6 of 181 body rows), cutting it mid-token, so the
     parts are concatenated with nothing between them — a separator would change the
     merchant key and the dedupe fingerprint (ADR 004, ADR 006).
  4. **Opening and closing balances are derived** from the rows, because ICICI prints
     neither, and `build_default_registry()` was added to `registry.py` as Step 5's
     docstring promised. The `GenericCsvParser` signed-amount branch is left as a
     `TODO(dhruv)` task with three skipped tests.
