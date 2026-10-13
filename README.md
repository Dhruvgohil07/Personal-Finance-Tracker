# Kharcha — Personal Finance Tracker

[![CI](https://github.com/Dhruvgohil07/Personal-Finance-Tracker/actions/workflows/ci.yml/badge.svg)](https://github.com/Dhruvgohil07/Personal-Finance-Tracker/actions/workflows/ci.yml)

Upload your bank statements (CSV/PDF) and get categorized spending,
subscriptions, budgets and alerts — built for Indian banks.

**Status:** Phase 1 (backend MVP) — a working API you can drive from
`/docs`: register, log in, add a bank account, upload a real ICICI
statement, read your transactions, categorize them by hand, and see a
monthly summary and a category breakdown. No frontend yet (Phase 3) and no
automatic categorization yet (Phase 2). See
[docs/progress.md](docs/progress.md) and the
[walkthrough](#walkthrough-phase-1) below.

**Stack:** Python 3.12 · FastAPI · PostgreSQL 16 · SQLAlchemy 2 + Alembic ·
Redis + RQ · structlog · pytest · ruff · uv. Frontend (Next.js) from Phase 3.

## Run it locally

### Prerequisites
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (runs Postgres and Redis)
- [uv](https://docs.astral.sh/uv/) (installs Python 3.12 and the dependencies)

### 1. Configure
```bash
cp .env.example .env
```
Then open `.env` and replace the three placeholder secrets. The commands to
generate them are written next to each one. The app refuses to start with
the placeholders (on purpose).

> **Windows:** keep `127.0.0.1` in the URLs. `localhost` tries IPv6 first,
> which Docker doesn't answer, and the connection hangs.

### 2. Start Postgres and Redis
```bash
docker compose up -d
```
This also creates a separate `kharcha_test` database, so tests never touch
your development data.

### 3. Install, migrate, seed
```bash
cd backend
uv sync                       # creates .venv with Python 3.12 + all dependencies
uv run alembic upgrade head   # builds the database schema
uv run python -m app.db.seed  # loads system categories and common merchants (safe to re-run)
```

### 4. Run the API
```bash
uv run uvicorn app.main:app --reload
```
- Interactive API docs: <http://127.0.0.1:8000/docs>
- Health check: <http://127.0.0.1:8000/api/v1/health> — `200` when Postgres
  and Redis are reachable, `503` (with the failing check named) when not

## Walkthrough (Phase 1)

Everything below runs from <http://127.0.0.1:8000/docs>. Each endpoint has a
**Try it out** button; the only manual step is pasting your access token
once.

### 1. Register and log in

`POST /api/v1/auth/register`

```json
{ "email": "you@example.com", "password": "a long passphrase", "name": "Your Name" }
```

Answers `201` with your user. It does **not** return tokens — logging in is
a separate step ([ADR 005](docs/decisions/005-auth-tokens.md)).

`POST /api/v1/auth/login` with the same email and password answers `200`:

```json
{ "access_token": "eyJ...", "token_type": "bearer", "expires_in": 900 }
```

Copy `access_token`, click **Authorize** at the top right of `/docs`, paste
it, and every call below is authenticated. The token lasts 15 minutes; when
it expires, log in again (or call `POST /api/v1/auth/refresh`, which uses
the httpOnly cookie login already set).

> Login and register are limited to **5 requests per minute per IP**. If you
> get a `429`, wait a minute.

### 2. Add the account the statement belongs to

`POST /api/v1/accounts`

```json
{ "bank_code": "ICICI", "nickname": "Salary account",
  "account_type": "savings", "masked_number": "1234" }
```

- `bank_code`: `ICICI`, `HDFC`, `SBI` or `GENERIC` (any other bank)
- `account_type`: `savings`, `current` or `credit_card`
- `masked_number`: **the last 4 digits only** — the full number is never
  stored (SPEC §7.3). Use the real last 4 digits of the account the
  statement came from: the import checks them against the statement and
  refuses a mismatch with a `400`.

Keep the `id` from the response — that is your `account_id`.

### 3. Upload the statement

`POST /api/v1/uploads` is a multipart form, so `/docs` gives you a file
picker:

| Field | Value |
|---|---|
| `file` | your `.xls` statement (ICICI gives legacy Excel — [ADR 004](docs/decisions/004-xls-support.md)) |
| `account_id` | the id from step 2 |
| `column_mapping` | leave empty |

Answers `201` with what the import did:

```json
{ "status": "completed", "period_start": "2026-08-01", "period_end": "2026-08-31",
  "rows_parsed": 175, "rows_inserted": 175, "rows_duplicate": 0, ... }
```

`rows_parsed = rows_inserted + rows_duplicate`, always.

Things worth trying here:

- **Upload the same file again** → `409 CONFLICT`. The file's sha256 is
  unique per user, so the identical file is refused outright.
- **Upload an overlapping statement** (e.g. July–September when you already
  have August) → `201`, with the August rows counted in `rows_duplicate`
  and only the new ones in `rows_inserted`. That is dedupe by fingerprint
  ([ADR 006](docs/decisions/006-dedupe-fingerprint.md)) and it is what makes
  imports idempotent.
- **Upload a PDF** → `400 INVALID_STATEMENT`. PDF parsing is Phase 2.
- `column_mapping` is only for a CSV that no bank parser recognises. It
  names the columns by their **header label**, so a headerless CSV is not
  supported in Phase 1. `/docs` shows the exact example:

  ```json
  { "date_column": "Date", "description_column": "Narration",
    "date_format": "%d/%m/%Y", "debit_column": "Withdrawal Amt.",
    "credit_column": "Deposit Amt.", "balance_column": "Closing Balance" }
  ```

  A mapping sent with a file a bank parser *does* recognise is ignored —
  the real parser always wins.

> Uploads are limited to **10 per hour per user**, and a file over
> `MAX_UPLOAD_MB` (default 10 MB) is a `413`.

### 4. Read your transactions

`GET /api/v1/transactions` returns the newest 50 first. Every filter is
optional and they combine with AND: `from`, `to`, `account_id`,
`category_id`, `direction`, `q`, `min_amount`, `max_amount`, `cursor`,
`limit`.

```
GET /api/v1/transactions?from=2026-08-01&to=2026-08-31&direction=debit&limit=20
```

- `min_amount` / `max_amount` are in **rupees** (`500.50`); every other
  money value in the API is integer paise.
- `q` searches the cleaned-up narration, so `zomato` finds
  `UPI/ZOMATO ONLINE/...` whatever case you type.
- Paging is by **cursor, not page number**: pass the `next_cursor` you were
  given back as `?cursor=...`, and stop when it is `null`
  ([ADR 009](docs/decisions/009-keyset-pagination.md)). There is no total
  count and no "page 3 of 20" — deliberately.

Each row carries `amount_paise` (canonical) and `amount_display`
(`"₹1,250.50"`, for reading).

### 5. Categorize one by hand

`GET /api/v1/categories` lists the 25 seeded system categories plus any of
your own. Copy the `id` of one, then:

`PATCH /api/v1/transactions/{id}`

```json
{ "category_id": "<a category id>", "notes": "dinner with family" }
```

Send only the fields you want to change. An explicit `null` clears either
one. The category is recorded as `category_source: "user_manual"`, which is
what stops the Phase 2 categorizer overwriting your correction.

**One behaviour worth understanding:** choosing a category whose `kind` is
`transfer` (e.g. *Self Transfer*) also sets `is_self_transfer: true`, and
any other category clears it. The insights read that boolean, so filing
your credit-card payment under *Self Transfer* is what removes it from your
spending — the category you can see and the totals can never disagree.

### 6. See the insights

`GET /api/v1/insights/summary?month=2026-08` — omit `month` for the current
month (in IST):

```json
{ "month": "2026-08", "from": "2026-08-01", "to": "2026-08-31",
  "current":  { "income_paise": 5000000, "spend_paise": 182050,
                "net_paise": 4817950, "transaction_count": 4,
                "income_display": "₹50,000.00", "net_display": "₹48,179.50" },
  "previous": { "month": "2026-07", "...": "..." },
  "income_change_pct": 25.0, "spend_change_pct": 82.1 }
```

A `*_change_pct` of `null` means the previous month had nothing to compare
against — growth from zero has no percentage, and `null` says so instead of
inventing a `0`.

`GET /api/v1/insights/categories?from=2026-08-01&to=2026-08-31` — spending
per category, biggest first. `from` and `to` are both inclusive and are
either both given or both left out (with neither, the current month).
Transactions you have not categorized arrive as one row with
`category_id: null`, so the parts always add up to `total_spend_paise`.

Both endpoints exclude self transfers, and the breakdown is debits only —
so a salary credit never appears there
([ADR 010](docs/decisions/010-insights-aggregation.md)).

### What Phase 1 deliberately does not do

| Missing | Arrives in |
|---|---|
| PDF statements, password-protected files | Phase 2 |
| Automatic categorization, merchants, UPI/VPA and channel detection | Phase 2 |
| Balance reconciliation, `GET /uploads`, deleting an upload | Phase 2 |
| `apply_to_similar` on a PATCH (answers `422` today) | Phase 2 |
| `/insights/trend`, `/insights/top-merchants` | Phase 2 |
| Creating your own categories, `/me`, `DELETE /me`, CSV export | Phase 2 |
| Web UI | Phase 3 |
| Recurring payments, budgets, alerts | Phases 4–5 |

## Tests and lint

Run from `backend/`:

```bash
uv run ruff check .                  # lint
uv run ruff format --check .         # formatting
uv run pytest                        # all tests (needs docker compose up)
uv run pytest -m "not integration"   # unit tests only, no Docker needed
uv run pytest --cov                  # with a coverage report
```

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs the same
commands on every push to `main` and every pull request, against real
Postgres 16 and Redis 7 containers.

## Project layout

```
backend/
  app/
    main.py        app factory: middleware, error handlers, routers
    core/          config, logging (with redaction), errors, middleware,
                   security (argon2id + JWT), rate limiting
    api/v1/        HTTP routes (thin: validate -> call a service -> respond)
    services/      business logic: auth, accounts, imports, transactions,
                   categories, insights
    parsers/       PURE statement parsing: no DB, no FastAPI, no Redis.
                   readers (file type, CSV, xls) -> a bank parser
                   (icici / generic_csv) -> normalize -> fingerprint
    models/        SQLAlchemy tables
    schemas/       Pydantic request/response models
    db/            engine + sessions, seed data
    utils/         money.py  Decimal -> paise parsing, INR formatting
                   dates.py  IST clock + the Month value object
                   cursor.py opaque keyset pagination cursors
  alembic/         database migrations (append-only)
  tests/unit/  tests/integration/
docs/
  SPEC.md          full specification
  decisions/       architecture decision records (ADRs)
  plans/           per-phase plans
  progress.md      where the project is, and every decision made so far
  request-lifecycle.md   how a request flows through FastAPI
```

`app/parsers/` is deliberately a **pure** package: a test asserts it
imports no FastAPI, SQLAlchemy or Redis. That is what lets the whole
parsing pipeline be unit tested without a database, and what will let the
Phase 2 RQ worker reuse it unchanged.

## Design principles

- **Money is never a float** — integer paise in `BIGINT` columns
  ([ADR 001](docs/decisions/001-money-as-paise.md)).
- **Every query is scoped by `user_id`**; cross-user access returns 404.
- **No sensitive data in logs** — amounts, narrations, passwords and tokens
  are redacted; request logs never include query strings
  ([ADR 003](docs/decisions/003-request-logging.md)).
- **Statement passwords are never stored** — not in the DB, the job queue
  or logs.
- **Imports are idempotent** — re-uploading a statement never duplicates
  transactions. The identical file is a `409`; an overlapping statement
  inserts only its new rows, because each row has a content fingerprint
  that is unique per account
  ([ADR 006](docs/decisions/006-dedupe-fingerprint.md)).
- **Only the last 4 digits of an account number are stored**, and the
  import checks them against the statement before writing anything.
- **Lists page by cursor, never by `OFFSET`** — a stable position in a list
  that grows at the newest end
  ([ADR 009](docs/decisions/009-keyset-pagination.md)).
- **Aggregates are measured, not assumed** — every reporting query is
  checked with `EXPLAIN (ANALYZE, BUFFERS)` and the findings recorded,
  including the two cases where an index turned out not to serve the query
  it was added for ([ADR 010](docs/decisions/010-insights-aggregation.md)).
- **Dates are dates** — `DATE`, never a timestamp, so "transactions in
  August" cannot depend on the timezone the row was read in. The app's own
  clock is Asia/Kolkata.

## Privacy

Never commit real bank statements. Put them in `samples/` (git-ignored).
Test fixtures are synthetic.
