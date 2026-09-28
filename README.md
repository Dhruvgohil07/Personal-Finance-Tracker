# Kharcha — Personal Finance Tracker

[![CI](https://github.com/Dhruvgohil07/Personal-Finance-Tracker/actions/workflows/ci.yml/badge.svg)](https://github.com/Dhruvgohil07/Personal-Finance-Tracker/actions/workflows/ci.yml)

Upload your bank statements (CSV/PDF) and get categorized spending,
subscriptions, budgets and alerts — built for Indian banks.

**Status:** Phase 0 (foundation) — backend skeleton with config, logging,
error handling, database + migrations + seed data, a health check and CI.
No product features yet. See [docs/progress.md](docs/progress.md).

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
    core/          config, logging (with redaction), errors, middleware
    api/v1/        HTTP routes (thin: validate -> call a service -> respond)
    services/      business logic
    models/        SQLAlchemy tables
    schemas/       Pydantic request/response models
    db/            engine + sessions, seed data
    utils/money.py Decimal -> paise parsing, INR formatting
  alembic/         database migrations (append-only)
  tests/unit/  tests/integration/
docs/
  SPEC.md          full specification
  decisions/       architecture decision records (ADRs)
  plans/           per-phase plans
  request-lifecycle.md   how a request flows through FastAPI
```

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
  transactions.

## Privacy

Never commit real bank statements. Put them in `samples/` (git-ignored).
Test fixtures are synthetic.
