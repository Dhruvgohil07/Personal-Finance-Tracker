# Phase 0 — Foundation

## Goal
A running, tested backend skeleton that every later phase builds on:
config, logging, error handling, database + migrations + seeds, a health
check, and CI. No product features yet.

## Done when
- `docker compose up -d` + `uvicorn app.main:app` runs locally
- `/docs` loads and `GET /api/v1/health` is green (DB + Redis reachable)
- `ruff check` and `pytest` pass locally and in GitHub Actions CI
- The FastAPI request lifecycle and dependency injection have been explained

## Decisions made during planning

| Decision | Option chosen | Why |
|---|---|---|
| Python version | 3.12, pinned with `.python-version` | Matches the spec, CI and deployment; `requires-python = ">=3.12"` alone let uv pick 3.13 |
| RQ worker on Windows | Run as a Docker service in `docker-compose.yml` (added in Phase 2) | RQ's default worker needs `fork()`, which Windows lacks; Docker also mirrors production |
| Schema scope | Add tables phase by phase, not the full §5 schema up front | Smaller, reviewable migrations and a migration history that is easy to explain |
| Extra dependencies | `python-multipart`, `email-validator` added | Required by FastAPI file uploads and Pydantic `EmailStr` |
| Service ports | Bound to `127.0.0.1` only | Local DB/Redis not reachable from the network |
| Test database | Separate `kharcha_test` DB, created by a Postgres init script | Tests can wipe tables without touching dev data |
| `categories.user_id` before `users` exists | Column now, FK added in Phase 1 | Final table shape from day one (ADR 002) |
| Enum storage | `VARCHAR` + `CHECK` (non-native) | Adding values is a simple migration (ADR 002) |
| System category uniqueness | `UNIQUE NULLS NOT DISTINCT (user_id, slug)` | One constraint; seed upserts on it (ADR 002) |

## Steps

Each step ends with `ruff` + `pytest`, an explanation, a "What you should
understand now" summary, a progress update and a proposed commit.

### Step 1 — Tooling and infrastructure
- `backend/pyproject.toml` — ruff, pytest and coverage settings
- `docker-compose.yml` — Postgres 16 + Redis 7, volumes, healthchecks
- `docker/postgres/init/01-create-test-db.sql` — creates `kharcha_test`
- `.env.example` — all §14 settings with placeholders
- `.gitignore`

### Step 2a — Config
- `backend/app/core/config.py` — pydantic-settings `Settings`; fail fast on
  missing/invalid values (e.g. `FILE_ENCRYPTION_KEY` must decode to 32 bytes)
- `backend/tests/unit/test_config.py`

### Step 2b — Logging with redaction
- `backend/app/core/logging.py` — structlog JSON logs + processor that
  redacts sensitive keys (amount, narration, password, token, balance, ...)
- `backend/tests/unit/test_logging.py`

### Step 2c — Errors
- `backend/app/core/errors.py` — `AppError` + global handlers producing
  `{"error": {"code", "message", "details"}}` for app, validation and
  unexpected errors
- `backend/tests/unit/test_errors.py`

### Step 2d — Money helpers
- `backend/app/utils/money.py` — `Decimal` → integer paise parser, INR
  lakh-grouping formatter
- `backend/tests/unit/test_money.py` — commas, Cr/Dr, bracketed negatives,
  blanks, no floats
- TODO(dhruv): one parsing edge-case test and one formatting edge-case test

### Step 3 — Database
- `backend/app/db/base.py`, `backend/app/db/session.py` — engine,
  `SessionLocal`, `get_db` dependency
- `backend/app/models/` — `categories`, `merchants` only
- `backend/alembic.ini`, `backend/alembic/` — first migration: extensions
  `pgcrypto`, `citext`, `pg_trgm` + the two tables
- `backend/app/db/seed.py` — idempotent seed of system categories and
  common Indian merchants
- `backend/app/db/seed_data.py` — the seed data, separate so it can be
  unit tested without a database
- Tests for the seed (runs twice without duplicating rows), a migration test
  (`alembic check` + downgrade/upgrade), `docs/decisions/002-database-schema-conventions.md`
- TODO(dhruv): add 3 merchants + `test_merchant_categories_exist`

### Step 4 — App and health check
- `backend/app/main.py` — app factory, error handlers, strict CORS,
  security headers middleware
- `backend/app/api/v1/health.py` — `GET /api/v1/health` (200 / 503)
- Tests: health endpoint, error response shape
- TODO(dhruv): add the app version to the `/health` response

### Step 5 — CI and docs
- `.github/workflows/ci.yml` — ruff + pytest with Postgres/Redis service
  containers
- `docs/decisions/001-money-as-paise.md`
- `README.md` — how to run locally
- Written explanation: FastAPI request lifecycle and dependency injection

## Change log
- Step 2 split into 2a (config), 2b (logging), 2c (errors), 2d (money) at
  the user's request, stopping after each for review and a commit.
