# Progress

## Current phase
Phase 0 — Foundation (plan: docs/plans/phase-0.md)

## Completed
- [x] Step 1: Tooling and infrastructure — pyproject.toml, docker-compose, .env.example, .gitignore
- [x] Step 2a: Config — app/core/config.py (fail-fast validation, secrets hidden)
- [x] Step 2b: Logging — app/core/logging.py (structlog JSON, key-based redaction)
- [x] Step 2c: Errors — app/core/errors.py (AppError + global handlers, one error shape)
- [x] Step 2d: Money helpers — app/utils/money.py (Decimal → paise parser, INR lakh formatter)
- [x] Step 3: Database — session, categories/merchants models, migration 0001, idempotent seed
- [x] Step 4: App and health check — app factory, strict CORS, security headers, GET /api/v1/health

## In progress
- (none)

## Next up
- Step 5: CI and docs (GitHub Actions, ADR 001, README, request lifecycle write-up)

## Open decisions / questions
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
- DB conventions → decided: VARCHAR+CHECK enums, NULLS NOT DISTINCT, user_id FK added in Phase 1 (ADR 002)
- Category kinds → open: "Transfers to People", "Investments", "Refunds" and "Uncategorized" kinds
  are a first guess; revisit when building insights (§9.1). Changing them = edit seed_data.py + re-seed
- Merchant key format → open: confirm against real narrations in Phase 2 (§6.5)

## My TODO(dhruv) tasks
- [x] Nested job-password redaction test (Step 2b, tests/unit/test_logging.py)
- [x] Money parsing + formatting edge-case tests (Step 2d)
- [x] 3 merchants + `test_merchant_categories_exist` (Step 3)
- [x] App version field in /health (Step 4) — done together; `app.__version__` + pyproject match test

## Known issues
- Windows: use `127.0.0.1`, not `localhost`, in DB/Redis URLs (IPv6 `::1` hangs with Docker);
  engine has a 10 s connect timeout so this fails loudly instead of hanging
- Run tests from `backend/` with the venv Python, not Anaconda's `python`
