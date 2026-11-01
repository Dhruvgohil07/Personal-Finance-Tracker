# Kharcha — Personal Finance Tracker

[![CI](https://github.com/Dhruvgohil07/Personal-Finance-Tracker/actions/workflows/ci.yml/badge.svg)](https://github.com/Dhruvgohil07/Personal-Finance-Tracker/actions/workflows/ci.yml)

Upload your bank statements (CSV/PDF) and get categorized spending,
subscriptions, budgets and alerts — built for Indian banks.

**Status:** Phase 2 (real-world imports) — a working API you can drive from
`/docs`: register, log in, add a bank account, upload a real ICICI
statement (`.xls` or PDF, password-protected or not), and a background
worker imports it, checks its balances and categorizes most transactions
automatically. Read your transactions, correct a category (and teach a rule
for similar ones), and see a monthly summary and a category breakdown, with
transfers between your own accounts left out. No frontend yet (Phase 3).
See [docs/progress.md](docs/progress.md), the
[walkthrough](#walkthrough) below and the
[Phase 2 acceptance run](docs/phase-2-acceptance.md).

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

### 2. Start Postgres, Redis and the worker
```bash
docker compose up -d --build
```
This also creates a separate `kharcha_test` database, so tests never touch
your development data.

The `worker` service imports uploaded statements in the background (RQ
needs `fork()`, so it runs in Docker, not on Windows — [ADR
013](docs/decisions/013-upload-processing-worker.md)). It reads the same
`.env` as the API, so both use the same `FILE_ENCRYPTION_KEY`. After
changing backend code, rebuild both with `docker compose up -d --build worker scheduler`;
read its logs with `docker compose logs -f worker`. Next to it, the
`scheduler` service enqueues an hourly `cleanup` job (stale uploads,
expired files, dead refresh-token families). Without a running
worker, uploads stay `pending`.

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

## Walkthrough

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
  a mismatch ends as a `failed` upload.

Keep the `id` from the response — that is your `account_id`.

### 3. Upload the statement

`POST /api/v1/uploads` is a multipart form, so `/docs` gives you a file
picker:

| Field | Value |
|---|---|
| `file` | your ICICI statement: `.xls` (legacy Excel — [ADR 004](docs/decisions/004-xls-support.md)) or PDF ([ADR 019](docs/decisions/019-pdf-parsing.md)) |
| `account_id` | the id from step 2 |
| `column_mapping` | leave empty |
| `password` | only for a password-protected PDF; otherwise leave empty |

Answers `202 Accepted` with the upload still `pending`: the worker imports
it in the background. Copy its `id` and poll `GET /api/v1/uploads/{id}`
until `status` is final (a statement takes a second or two):

```json
{ "status": "completed", "period_start": "2026-08-01", "period_end": "2026-08-31",
  "rows_parsed": 175, "rows_inserted": 175, "rows_duplicate": 0, ... }
```

`rows_parsed = rows_inserted + rows_duplicate`, always. The balance column
is checked row by row (SPEC §6.4): if it does not add up, the rows are
still imported but `status` is `needs_review`, and
`reconciliation_issues` lists the file's row numbers to look at
(`null` means the statement had no balance column to check). If the file
cannot be imported, `status` is `failed` and `error_message` says why (an
unreadable row, a statement for a different account). The same file can
then be uploaded again.

While the upload is unfinished, responses carry a `Retry-After` header
(seconds to wait before polling again). Polling has its own rate limit, so
it does not eat into the general one.

`GET /api/v1/uploads` lists your uploads, newest first.
`DELETE /api/v1/uploads/{id}` undoes an import: the upload and every
transaction it added are removed. It answers `409` while the upload is
being imported, and also when a *later* overlapping statement relies on
this one's rows — delete the later one first.

Things worth trying here:

- **Upload the same file again** → `409 CONFLICT`. The file's sha256 is
  unique per user, so the identical file is refused outright.
- **Upload an overlapping statement** (e.g. July–September when you already
  have August) → `202`, then `completed` with the August rows counted in `rows_duplicate`
  and only the new ones in `rows_inserted`. That is dedupe by fingerprint
  ([ADR 006](docs/decisions/006-dedupe-fingerprint.md)) and it is what makes
  imports idempotent.
- **Upload the same statement as `.xls` and as PDF** → the second one is
  100% `rows_duplicate`: both formats rebuild the narration the same way,
  so the fingerprints agree.
- **Upload a password-protected PDF without `password`** → `409
  PASSWORD_REQUIRED`, with the waiting upload's id in
  `error.details.upload_id`; `GET /uploads/{id}` shows `needs_password`.
  Send the password to `POST /api/v1/uploads/{id}/password` as
  `{"password": "..."}` → `202`, then poll as above. A wrong password is
  `400 INCORRECT_PASSWORD` and you can try again (5 tries a minute). The
  PDF is unlocked in memory during that request and stored without its
  password, so the password is never saved anywhere — not in the
  database, not in the job queue, not in the logs
  ([ADR 020](docs/decisions/020-statement-passwords.md)). Sending
  `password` with the upload itself skips the 409. An upload left waiting
  expires after 24 hours.
- `column_mapping` is only for a CSV that no bank parser recognises. It
  names the columns by their **header label**, so a headerless CSV is not
  supported. `/docs` shows the exact example:

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
(`"₹1,250.50"`, for reading), plus what the importer read from the
narration: `channel` (`upi`, `imps`, `atm`, ...), `counterparty_vpa` and
`merchant_key`.

Most rows arrive **already categorized**. `category_source` says how
([ADR 016](docs/decisions/016-categorizer-chain.md)):

| `category_source` | Meaning |
|---|---|
| `user_manual` | you set it by hand; nothing automatic ever changes it |
| `user_rule` | one of your rules matched (see step 5) |
| `merchant` | a known merchant, from the seeded dictionary |
| `keyword` | a word in the narration (refund, ATM, interest, ...) |
| `heuristic` | a guess: a UPI payment to or from a person, or one half of a transfer between your own accounts (`is_self_transfer: true`) |
| `none` | nothing matched — this is what "needs your attention" means |

On the real ICICI statements, 51% (August `.xls`) and 75% (October PDF)
of the rows were categorized this way, with no LLM
([acceptance run](docs/phase-2-acceptance.md)).

### 5. Categorize one by hand

`GET /api/v1/categories` lists the 25 seeded system categories plus any of
your own. Copy the `id` of one, then:

`PATCH /api/v1/transactions/{id}`

```json
{ "category_id": "<a category id>", "notes": "dinner with family" }
```

Send only the fields you want to change. An explicit `null` clears either
one. The category is recorded as `category_source: "user_manual"`, which is
what stops the categorizer overwriting your correction.

Add `"apply_to_similar": true` to also **teach a rule**: the same
counterparty (by UPI id or merchant) gets that category from now on, and
your existing rows are re-run at once. `GET/POST/PATCH/DELETE
/api/v1/rules` manage the rules directly
([ADR 017](docs/decisions/017-user-rules.md)).

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
([ADR 010](docs/decisions/010-insights-aggregation.md)). A transfer
between two of your accounts (money out of one, the same amount into the
other within two days) is detected on import and marked as a self
transfer automatically ([ADR 018](docs/decisions/018-self-transfer-matching.md)).

### What is not built yet

| Missing | Arrives in |
|---|---|
| Web UI, and search that uses its index (`q=`) | Phase 3 |
| Creating your own categories, `/me`, `DELETE /me`, CSV export | Phase 3 |
| `/insights/trend`, `/insights/top-merchants` | later phase (see [SPEC](docs/SPEC.md)) |
| LLM categorization for what the rules cannot place | Phase 4 |
| Recurring payments, budgets, alerts | Phases 4–5 |
| Parsers for banks other than ICICI | when real samples exist ([SPEC §6.2](docs/SPEC.md)) |

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
    services/      business logic: auth, accounts, imports, uploads,
                   transactions, categories, categorization, rules,
                   insights, cleanup
    parsers/       PURE statement parsing: no DB, no FastAPI, no Redis.
                   readers (file type, CSV, xls, PDF) -> a bank parser
                   (icici / icici_pdf / generic_csv) -> normalize (+ the
                   narration's channel, VPA, merchant) -> fingerprint;
                   reconcile (balance check), pdf_unlock (passwords)
    categorizer/   PURE categorization chain, rule learning, self-transfer
                   matching
    workers/       RQ queue, jobs, worker and scheduler entry points
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

`app/parsers/` and `app/categorizer/` are deliberately **pure** packages: a
test asserts they import no FastAPI, SQLAlchemy or Redis. That is what lets
the whole parsing and categorizing pipeline be unit tested without a
database, and what let the RQ worker reuse it unchanged.

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
