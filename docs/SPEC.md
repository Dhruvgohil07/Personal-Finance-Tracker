# Kharcha — Full Project Specification

## 1. Problem & Vision

In India, most spending now happens through UPI in dozens of small payments per week, spread across multiple bank accounts and apps. Bank apps show raw transaction lists with cryptic narrations like `UPI/DR/4123.../ZOMATO/...`, so people have no clear answer to "where did my money go this month?", forget subscriptions they're paying for, and miss unusual or duplicate debits.

**Kharcha** lets a user upload the bank statements they already get (CSV or PDF, including password-protected PDFs), and turns them into a clean, categorized, searchable picture of their finances: monthly spend by category, recurring payments and subscriptions, budgets, and alerts for unusual activity.

**Key principle: it must work for a single user on day one.** No bank APIs, no partnerships, no crowd data. Everything comes from the user's own statements.

### Target users
- Students and young professionals using UPI heavily
- Families who want to see household spending across 2–3 bank accounts
- Me (the developer) — I am the first real user

### Goals
- Accurate import: no duplicates, no missed rows, balances reconcile
- Useful categorization with minimal manual effort, that improves from user corrections
- Clear insights: monthly summary, trends, top merchants, subscriptions
- Privacy and security treated as a core feature, not an afterthought

### Non-goals (do NOT build these)
- No bank API / Account Aggregator integration
- No moving money, payments, or UPI initiation
- No investment advice or credit scoring
- INR only (no multi-currency) for now
- No native mobile app (responsive web only)

---

## 2. Tech Stack

### Backend (Python)
| Layer | Choice |
|---|---|
| Language | Python 3.12, with type hints everywhere |
| Dependency management | `uv` (or `pip` + `venv` if uv causes issues) with `pyproject.toml` |
| API framework | FastAPI |
| Validation / schemas | Pydantic v2 (request/response models) + pydantic-settings (config) |
| Database | PostgreSQL 16 |
| ORM / DB access | SQLAlchemy 2.0 (synchronous, with `psycopg` v3 driver); raw SQL via `text()` allowed for analytics queries |
| Migrations | Alembic |
| Background jobs | RQ (Redis Queue) + Redis |
| Periodic jobs | APScheduler running in a small scheduler process that enqueues RQ jobs |
| CSV parsing | Python `csv` module (pandas only for analytics if genuinely helpful) |
| PDF parsing | pdfplumber (supports password-protected PDFs and table extraction) |
| Password hashing | argon2-cffi |
| JWT | PyJWT |
| Encryption | `cryptography` (AES-256-GCM) |
| Rate limiting | slowapi (Redis-backed) |
| LLM | Groq Python SDK, used only as a fallback categorizer (model name from env) |
| Logging | structlog (structured JSON logs) |
| Lint / format | ruff |
| Testing | pytest, pytest-cov, FastAPI TestClient (httpx2) |
| Email (Phase 5) | Resend |

### Frontend (Phase 3 onward)
| Layer | Choice |
|---|---|
| Framework | Next.js (App Router) in **plain JavaScript** (not TypeScript — I'm still learning JS) |
| Styling | Tailwind CSS |
| Data fetching | TanStack Query |
| Charts | Recharts |

Until the frontend phase, all features are tested through FastAPI's auto-generated docs at `/docs` (Swagger UI).

### Infrastructure
- docker-compose for local Postgres + Redis
- GitHub Actions CI: ruff, pytest

Ask me before adding any dependency not listed here.

---

## 3. Repository Structure

```
kharcha/
├── backend/
│   ├── app/
│   │   ├── main.py                # FastAPI app creation, middleware, router registration
│   │   ├── core/
│   │   │   ├── config.py          # pydantic-settings: loads and validates env vars
│   │   │   ├── security.py        # password hashing, JWT create/verify, encryption helpers
│   │   │   ├── logging.py         # structlog setup (with sensitive-field redaction)
│   │   │   └── errors.py          # custom exceptions + global exception handlers
│   │   ├── db/
│   │   │   ├── session.py         # engine + SessionLocal + get_db dependency
│   │   │   └── base.py            # SQLAlchemy declarative Base
│   │   ├── models/                # SQLAlchemy ORM models (one file per table group)
│   │   ├── schemas/               # Pydantic request/response models
│   │   ├── api/
│   │   │   ├── deps.py            # shared dependencies (get_current_user, get_db)
│   │   │   └── v1/                # routers: auth.py, accounts.py, uploads.py, transactions.py, ...
│   │   ├── services/              # business logic (routes call services; services call DB)
│   │   ├── parsers/               # PURE statement parsers + normalization (no FastAPI/DB imports)
│   │   ├── categorizer/           # PURE categorization, recurring & anomaly detection logic
│   │   ├── utils/
│   │   │   └── money.py           # paise parsing/formatting helpers
│   │   └── workers/
│   │       ├── queue.py           # Redis connection + RQ queues
│   │       ├── jobs.py            # job functions
│   │       └── scheduler.py       # APScheduler process for periodic jobs
│   ├── alembic/                   # migrations
│   ├── tests/
│   │   ├── unit/                  # parsers, money, categorizer, recurring, anomalies
│   │   ├── integration/           # API tests against real Postgres/Redis
│   │   └── fixtures/              # SYNTHETIC / anonymized statement files only
│   ├── pyproject.toml
│   └── alembic.ini
├── frontend/                      # Next.js app (created in Phase 3)
├── docs/
│   ├── decisions/                 # ADRs: 001-money-as-paise.md, 002-dedupe-fingerprint.md, ...
│   └── architecture.md            # diagram + data flow
├── samples/                       # REAL statements for local testing — MUST be in .gitignore
├── docker-compose.yml             # postgres + redis (+ api + worker optional)
├── .env.example
└── CLAUDE.md
```

Layering rule: **routers → services → models/DB**. `parsers/` and `categorizer/` must not import FastAPI or SQLAlchemy, so they are easy to unit test in isolation.

---

## 5. Data Model (PostgreSQL)

Use UUID primary keys (`gen_random_uuid()` or Python `uuid4`), `created_at`/`updated_at` timestamptz columns, and foreign keys with `ON DELETE CASCADE` from `users` so account deletion removes everything. Enable extensions `pgcrypto`, `citext`, `pg_trgm` in the first migration.

### users
- `id`, `email` (citext, unique), `password_hash`, `name`
- `settings` jsonb — e.g. `{ "ai_enabled": true, "weekly_email": false }`
- `created_at`, `updated_at`

### refresh_tokens
- `id`, `user_id` FK, `token_hash` (unique, store only a hash), `family_id` (for reuse detection), `expires_at`, `revoked_at`, `created_at`
- If a revoked token is reused → revoke the whole family (token theft detection).

### accounts (the user's bank accounts / cards)
- `id`, `user_id` FK, `bank_code` (e.g. `HDFC`, `SBI`, `ICICI`, `GENERIC`), `nickname`, `account_type` enum(`savings`, `current`, `credit_card`)
- `masked_number` — last 4 digits ONLY. Never store full account numbers.
- `unique(user_id, bank_code, masked_number)`

### statement_uploads
- `id`, `user_id` FK, `account_id` FK, `original_filename`, `file_type` enum(`csv`, `pdf`), `file_sha256`
- `status` enum(`pending`, `processing`, `needs_password`, `needs_review`, `completed`, `failed`)
- `period_start`, `period_end`, `rows_parsed`, `rows_inserted`, `rows_duplicate`, `reconciliation_errors` int, `error_message`
- `created_at`, `completed_at`
- `unique(user_id, file_sha256)` → re-uploading the exact same file is rejected immediately with a friendly message.

### upload_blobs (temporary)
- `upload_id` PK/FK, `encrypted_content` bytea, `nonce`, `created_at`
- Holds the file between API and worker (already unlocked if it was password-protected, then encrypted by the app with AES-256-GCM). **Deleted as soon as processing finishes or fails.** A cleanup job also deletes any blob older than 24h.
- Rationale: API and worker are separate processes/services with no shared disk on free hosting; Postgres is the simplest shared store. (Could move to S3/R2 later — write an ADR.)

### categories
- `id`, `user_id` FK nullable (NULL = system default category), `name`, `slug`, `parent_id` nullable, `kind` enum(`expense`, `income`, `transfer`), `icon`, `color`
- `unique(user_id, slug)`
- Seed system defaults: Food & Dining, Groceries, Shopping, Transport, Fuel, Travel, Bills & Utilities, Mobile & Internet, Rent, Entertainment, Subscriptions, Health, Education, EMI & Loans, Investments, Insurance, Fees & Charges, Cash Withdrawal, Self Transfer, Transfers to People, Salary, Refunds, Interest, Other Income, Uncategorized.

### merchants (global dictionary, no personal data)
- `id`, `normalized_key` (unique, e.g. `ZOMATO`), `display_name`, `default_category_slug`
- Seed with common Indian merchants (Zomato, Swiggy, Blinkit, Zepto, Amazon, Flipkart, Uber, Ola, Rapido, IRCTC, Netflix, Spotify, Jio, Airtel, etc.).

### merchant_category_cache (LLM result cache, global)
- `normalized_key` PK, `category_slug`, `confidence` numeric, `source` (`llm`), `model`, `created_at`
- Only merchant-like keys are cached globally. Never cache person-to-person narrations (see §8.3).

### transactions
- `id`, `user_id` FK, `account_id` FK, `upload_id` FK
- `txn_date` DATE, `value_date` DATE nullable
- `amount_paise` BIGINT `CHECK (amount_paise > 0)`, `direction` enum(`debit`, `credit`)
- `balance_after_paise` BIGINT nullable
- `raw_description` text, `normalized_description` text
- `channel` enum(`upi`, `card`, `neft`, `imps`, `rtgs`, `atm`, `cheque`, `ach_nach`, `interest`, `charges`, `other`)
- `counterparty_vpa` text nullable, `merchant_id` FK nullable
- `category_id` FK nullable, `category_source` enum(`user_manual`, `user_rule`, `merchant`, `keyword`, `llm`, `heuristic`, `none`), `category_confidence` numeric nullable
- `is_self_transfer` boolean default false, `notes` text nullable
- `fingerprint` text NOT NULL, `unique(account_id, fingerprint)`
- Indexes: `(user_id, txn_date DESC, id DESC)` for keyset pagination; `(user_id, category_id, txn_date)`; `(user_id, merchant_id)`; trigram GIN index on `normalized_description` for search.

### category_rules (user's own rules, learned from corrections)
- `id`, `user_id` FK, `match_type` enum(`merchant`, `vpa`, `contains`), `pattern`, `category_id` FK, `priority` int, `created_from_transaction_id` nullable, `created_at`

### recurring_payments
- `id`, `user_id` FK, `account_id` FK, `merchant_key`, `display_name`
- `avg_amount_paise`, `last_amount_paise`, `cadence` enum(`weekly`, `monthly`, `quarterly`, `yearly`)
- `occurrences` int, `last_seen_date`, `next_expected_date`
- `status` enum(`active`, `lapsed`, `dismissed`), `user_confirmed` boolean

### budgets
- `id`, `user_id` FK, `category_id` FK, `monthly_limit_paise` BIGINT, `alert_thresholds` int[] default `{80,100}`
- `unique(user_id, category_id)`

### alerts
- `id`, `user_id` FK, `type` enum(`budget_threshold`, `unusual_debit`, `possible_duplicate_charge`, `subscription_price_change`, `upcoming_recurring`)
- `transaction_id` nullable, `payload` jsonb, `dedupe_key` text, `read_at`, `created_at`
- `unique(user_id, dedupe_key)` → the same alert is never created twice (e.g. `budget:{category_id}:{2026-09}:80`).

### audit_log
- `id`, `user_id`, `action` (e.g. `login`, `upload_created`, `account_deleted`, `export`), `entity`, `entity_id`, `ip`, `user_agent`, `created_at`
- Never store financial values here.

---

## 6. Statement Ingestion Pipeline

### 6.1 Flow

```
Upload (API: POST /uploads)
  → validate (size ≤ 10 MB, extension + file signature/magic bytes, account belongs to user)
  → compute sha256 → reject if already uploaded by this user
  → if PDF is password-protected and no password given → status = needs_password, return 409 asking for password
  → if password given → open/unlock IN MEMORY during the request, then discard the password
  → encrypt file bytes with app key (AES-256-GCM) → store in upload_blobs
  → create statement_uploads row (pending) → enqueue RQ job process_statement(upload_id) — upload_id ONLY
Worker: process_statement
  → load + decrypt blob → detect bank format → parse → normalize
  → reconcile balances → compute fingerprints → insert all rows in ONE DB transaction
  → delete blob → set status completed / needs_review / failed
  → enqueue follow-up jobs: categorization → post-import analysis → alert evaluation
```

The client polls `GET /uploads/{id}` for progress.

Note on password-protected PDFs: pdfplumber opens them with a `password` argument. To avoid ever passing the password to the worker, the API unlocks the PDF in memory and stores an unlocked copy (then app-encrypted) — or extracts the text/table data during the request. Choose the simpler approach, explain the trade-off, and write an ADR.

### 6.2 Parsers (Strategy pattern + Registry)

```python
from typing import Protocol

class StatementParser(Protocol):
    bank_code: str
    file_types: tuple[str, ...]          # ("csv",) or ("pdf",) or both

    def detect(self, data: ParserInput) -> float:
        """Return confidence 0.0–1.0 that this parser handles the file."""

    def parse(self, data: ParserInput) -> ParsedStatement:
        """Return parsed rows; raise ParseError with row context on failure."""
```

- A `ParserRegistry` calls `detect()` on every registered parser and picks the highest confidence above a threshold. If none match, fall back to `GenericCsvParser`, which uses a user-provided column mapping (date column, description column, debit/credit columns or signed amount column, balance column, date format).
- Start with the banks I actually use. Build each parser from my real statements in `samples/` (gitignored), then create **anonymized fixtures** in `tests/fixtures/` for tests. **Do not guess bank formats — ask me for a sample when adding a new bank.**
- PDF parsing: use pdfplumber table extraction first; if a bank's layout doesn't extract cleanly, fall back to word positions (group words by y-coordinate into rows, map columns by header x-positions). Handle multi-line descriptions and repeated page headers/footers.
- Use dataclasses or Pydantic models: `ParsedStatement(period_start, period_end, opening_balance_paise, closing_balance_paise, rows: list[RawRow])`.

### 6.3 Deduplication (idempotent imports)

- Level 1: `file_sha256` unique per user → same file rejected instantly.
- Level 2: per-row `fingerprint` = sha256 of `account_id | txn_date | amount_paise | direction | normalized_description | balance_after_paise | occurrence_index`.
  - `occurrence_index` = the Nth identical row on that date within the statement (handles two genuine ₹20 chai payments on the same day when the statement has no balance column).
- Insert using PostgreSQL `INSERT ... ON CONFLICT (account_id, fingerprint) DO NOTHING` (SQLAlchemy's `postgresql.insert(...).on_conflict_do_nothing(...)`) and count duplicates → overlapping statements (e.g. Aug 1–31 and Aug 15–Sep 15) merge cleanly.
- Write an ADR explaining this design and its edge cases.

### 6.4 Balance reconciliation (correctness check)

When a balance column exists: for each row, `previous_balance ± amount` must equal `balance_after`. Also check opening + credits − debits = closing. If any row fails, keep the data but set status `needs_review` and report which rows look wrong. This catches parser bugs early — write tests for it.

### 6.5 Normalization

Implemented in `app/parsers/normalize.py`, pure and unit tested:
- Uppercase, collapse whitespace, strip long digit sequences (reference numbers), strip repeated bank prefixes
- Detect `channel` from prefixes/keywords (UPI, NEFT, IMPS, RTGS, POS/card, ATM/ATW, ACH/NACH, interest, charges)
- Extract UPI VPA with a regex like `[a-zA-Z0-9.\-_]+@[a-zA-Z]+`
- Produce a `merchant_key` candidate (e.g. `ZOMATO`) used by categorization and recurring detection
- Narration formats differ by bank — derive rules from real samples and cover each with a test.

---

## 8. Categorization Engine (Chain of Responsibility)

Located in `app/categorizer/`. Each handler either returns `(category_slug, source, confidence)` or passes to the next:

1. **User manual** — if the user already set this transaction's category, never overwrite it
2. **User rules** — `category_rules` ordered by priority (merchant key, VPA, or "description contains")
3. **Merchant dictionary** — `merchants.default_category_slug`
4. **Keyword rules** — built-in patterns (e.g. interest credit → Interest, `ATM`/`ATW` → Cash Withdrawal, `SALARY` → Salary, `NACH` + EMI → EMI & Loans, bank charges → Fees & Charges)
5. **Heuristic** — P2P detection (see 8.3) → Transfers to People
6. **LLM fallback** — only if `ai_enabled`; see 8.2
7. **Default** — Uncategorized

### 8.1 Learning from corrections
When the user changes a transaction's category with `apply_to_similar=true`, create a `category_rules` row and re-categorize matching past transactions (except `user_manual` ones) in a background job.

### 8.2 LLM categorization
- Batch unique uncached merchant keys (max ~50 per request)
- Ask the model for strict JSON: `[{"key": "...", "category": "<one of the allowed slugs>", "confidence": 0.0-1.0}]`
- Validate with Pydantic; unknown category or parse failure → Uncategorized. Retry on rate limits / 5xx with RQ's `Retry` and backoff.
- Cache results in `merchant_category_cache`; confidence < 0.6 → mark low confidence and surface for user review
- The app must work fully (minus this step) without a `GROQ_API_KEY`.

### 8.3 Person-to-person (P2P) detection
UPI transfers to individuals (VPA looks like a phone number or personal handle, no merchant match, no business keywords) are categorized as "Transfers to People" by heuristic and never sent to the LLM or the global cache, since they contain real people's names.

### 8.4 Self-transfer detection
A debit in one of the user's accounts and a credit of the same amount in another of their accounts within 0–2 days → mark both `is_self_transfer = true`, category Self Transfer. Self transfers are excluded from spending/income totals.

---

## 9. Insights, Recurring Payments & Alerts

### 9.1 Insights (SQL aggregations, exclude self transfers)
- Monthly summary: total income, total spend, net, vs previous month (% change)
- Spend by category for any date range
- 12-month trend (income vs spend per month)
- Top merchants by spend
- Write these as clear SQL (GROUP BY, date_trunc, window functions where useful). Verify with `EXPLAIN ANALYZE` that they use indexes; document findings in an ADR.

### 9.2 Recurring payment detection
Run after each import, per user:
- Group debits by `merchant_key` (+ account)
- A group is recurring if ≥ 3 occurrences, amounts within ±10% of the median, and intervals between payments cluster around 7 / 30 / 91 / 365 days (with tolerance, e.g. 25–35 days for monthly)
- Store cadence, average amount, last seen, `next_expected_date`
- A new payment > 10% above average → `subscription_price_change` alert
- If `next_expected_date` passes by more than one cadence tolerance with no payment → status `lapsed`
- User can confirm or dismiss detected subscriptions

### 9.3 Alerts
- `unusual_debit`: amount > max(3 × median debit for that category over last 90 days, ₹2,000 floor), only when ≥ 10 data points exist; also first-time merchant with a large amount
- `possible_duplicate_charge`: same merchant and same amount within 24 hours (different fingerprints)
- `budget_threshold`: category spend crosses 80% / 100% of the monthly budget
- `upcoming_recurring`: a confirmed subscription is due in the next 3 days
- All alerts use `dedupe_key` so they are created only once. Phase 5: optional weekly email summary.

---

## 10. REST API (`/api/v1`)

All responses JSON via Pydantic response models. Errors use a consistent shape: `{"error": {"code": "STRING_CODE", "message": "human readable", "details": ...}}` (implement global exception handlers). List endpoints use keyset (cursor) pagination on `(txn_date, id)`.

**Auth & user**
- `POST /auth/register`, `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout`
- `GET /me`, `PATCH /me/settings`, `DELETE /me`

**Accounts**
- `GET /accounts`, `POST /accounts`, `PATCH /accounts/{id}`, `DELETE /accounts/{id}`

**Uploads**
- `POST /uploads` (multipart: `file`, `account_id`, optional `password`, optional `column_mapping` JSON for generic CSV)
- `GET /uploads`, `GET /uploads/{id}` (status + counts + reconciliation issues)
- `POST /uploads/{id}/password`
- `DELETE /uploads/{id}` (removes the upload and the transactions it inserted)

**Transactions**
- `GET /transactions?from&to&account_id&category_id&direction&q&min_amount&max_amount&cursor&limit`
- `PATCH /transactions/{id}` (`category_id`, `notes`, `is_self_transfer`, `apply_to_similar: bool`)
- `GET /transactions/export.csv`

**Categories & rules**
- `GET/POST/PATCH/DELETE /categories` (user-defined only; system ones read-only)
- `GET/POST/PATCH/DELETE /rules`

**Insights**
- `GET /insights/summary?month=YYYY-MM`
- `GET /insights/categories?from&to`
- `GET /insights/trend?months=12`
- `GET /insights/top-merchants?from&to&limit`

**Recurring, budgets, alerts**
- `GET /recurring`, `PATCH /recurring/{id}` (confirm / dismiss)
- `GET/POST/PATCH/DELETE /budgets`, `GET /budgets/status?month=YYYY-MM`
- `GET /alerts?unread=true`, `PATCH /alerts/{id}/read`

**Health**
- `GET /health` (DB + Redis connectivity)

---

## 11. Background Jobs (RQ)

| Job | Trigger | Notes |
|---|---|---|
| `process_statement(upload_id)` | new upload | args = upload id only; `Retry(max=3)` with backoff; idempotent (safe to re-run) |
| `categorize_upload(upload_id)` / `recategorize_by_rule(rule_id)` | after import / new rule | batches LLM calls |
| `post_import_analysis(user_id)` | after categorization | self-transfer detection, recurring detection |
| `evaluate_alerts(user_id)` | after analysis; also daily via scheduler | budget, anomaly, upcoming recurring |
| `cleanup()` | hourly via scheduler | delete `upload_blobs` older than 24h, expired refresh tokens |
| `send_weekly_emails()` (Phase 5) | weekly via scheduler | only for opted-in users |

Every job must be safe to retry (idempotent), open its own DB session, and update `statement_uploads.status` on final failure with a user-friendly `error_message`. Run worker with `rq worker` and the scheduler as a separate small process (`python -m app.workers.scheduler`).

---

## 12. Frontend (Phase 3+, Next.js in plain JavaScript)

Pages:
- **Auth:** Login, Register
- **Onboarding:** add first account → upload first statement → see results (goal: value in under 2 minutes)
- **Dashboard:** this month's income/spend/net vs last month, category donut, 12-month trend, top merchants, unread alerts, upcoming subscriptions
- **Transactions:** filterable/searchable table, inline category edit with an "apply to similar?" prompt, notes, low-confidence badge
- **Uploads:** history with status, counts (inserted/duplicate), reconciliation issues, password prompt, generic CSV column-mapping wizard
- **Subscriptions:** detected recurring payments with confirm/dismiss, next due date, price change flags
- **Budgets:** monthly limits per category with progress bars
- **Alerts:** list, mark as read
- **Settings:** AI toggle (with plain explanation of what is sent), weekly email toggle, export CSV, delete account (typed confirmation)

UX rules: responsive/mobile-first, helpful empty states, loading and error states everywhere, money formatted with `Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR' })`, dark mode support.

Since I'm learning JavaScript, keep components simple, explain React concepts (state, effects, props, data fetching) as they come up, and leave small `// TODO(dhruv):` tasks for me.

---

## 13. Testing Strategy

- **Unit (most important):** money parsing, every bank parser against fixtures, normalization/channel/VPA extraction, fingerprint + occurrence index, balance reconciliation, each categorization handler, recurring detection (synthetic dated data), anomaly rules
- **Integration (TestClient + real Postgres/Redis from docker-compose, separate test database):** auth flow incl. refresh rotation and reuse detection, upload → process → transactions visible (run RQ jobs synchronously in tests), re-upload rejected, overlapping statements dedupe, IDOR test on every resource, delete account removes all rows
- **Security tests:** job args contain no password; logs contain no amounts/narrations (capture logs in a test)
- Target: `parsers/` and `categorizer/` ≥ 90% coverage; CI must pass before merging

---

## 14. Configuration (`.env.example`)

```
ENV=development
API_PORT=8000
FRONTEND_ORIGIN=http://localhost:3000
DATABASE_URL=postgresql+psycopg://kharcha:kharcha@localhost:5432/kharcha
TEST_DATABASE_URL=postgresql+psycopg://kharcha:kharcha@localhost:5432/kharcha_test
REDIS_URL=redis://localhost:6379/0
JWT_ACCESS_SECRET=change-me
JWT_REFRESH_SECRET=change-me
FILE_ENCRYPTION_KEY=base64-encoded-32-byte-key
GROQ_API_KEY=
GROQ_MODEL=
RESEND_API_KEY=
MAX_UPLOAD_MB=10
```

Load and validate with pydantic-settings at startup; fail fast with a clear error if anything required is missing.

---

## 15. Deployment (free tiers)

- Backend API → Render or Railway (web service running `uvicorn app.main:app`)
- Worker + scheduler → Render/Railway background worker(s) from the same repo
- Postgres → Neon
- Redis → Upstash (or Render/Railway Redis)
- Frontend → Vercel (Phase 3+)
- Run `alembic upgrade head` as a release step; seed system categories and merchants idempotently
- Document the full deployment in `README.md`

---

## 16. Build Roadmap

Each phase must end in a working, tested state. Phases 1–2 are backend-only and tested via `/docs`.

### Phase 0 — Foundation
Backend project scaffold (`pyproject.toml`, ruff, pytest), docker-compose (Postgres + Redis), config with pydantic-settings, structlog with redaction, global error handlers, DB session + first Alembic migration + seeds, `/health`, GitHub Actions CI.
**Done when:** `docker compose up` + `uvicorn` runs locally, `/docs` loads, `/health` is green, CI passes. Explain the FastAPI request lifecycle and dependency injection to me.

### Phase 1 — Backend MVP (usable by me via /docs)
Auth (register/login/refresh/logout), accounts CRUD, CSV upload for my primary bank + generic CSV with column mapping, money parsing, normalization, fingerprint dedupe, transactions list with filters + manual category edit, monthly summary + category breakdown endpoints. Processing may run synchronously inside a service function so it can move to RQ in Phase 2 without changing logic.
**Done when:** I can upload my real CSV statement through `/docs`, see correct transactions, re-upload without duplicates, and categorize manually.

### Phase 2 — Real-world imports
RQ worker, PDF parsing (incl. password-protected), encrypted upload blobs, balance reconciliation + `needs_review`, channel/VPA extraction, merchant dictionary + keyword categorization, user rules from corrections, self-transfer detection, parsers for my other banks, cleanup job.
**Done when:** PDFs from all my banks import correctly, overlapping statements merge cleanly, most transactions auto-categorize.

### Phase 3 — Frontend MVP
Next.js (plain JavaScript) app: auth pages, onboarding, upload page with status polling, transactions table with filters and category editing, basic dashboard. Deploy backend + frontend.
**Done when:** a family member can sign up, upload a statement, and understand their monthly spending without my help.

### Phase 4 — Intelligence
LLM fallback categorization with cache + review UI, P2P heuristic, recurring detection + Subscriptions page, budgets, full insights (trend, top merchants), CSV export.
**Done when:** a new statement is > 85% auto-categorized and my subscriptions are detected correctly.

### Phase 5 — Alerts, hardening, polish
Alerts (unusual debit, duplicate charge, budget, price change, upcoming recurring), weekly email summary, audit log, delete account, security review (rate limits, headers, IDOR tests), index review with `EXPLAIN ANALYZE`, README + architecture diagram + ADRs.
**Done when:** the app is stable for daily use and every design decision is documented.
