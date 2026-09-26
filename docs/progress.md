# Progress

## Current phase
Phase 0 — Foundation (plan: docs/plans/phase-0.md)

## Completed
- [x] Step 1: Tooling and infrastructure — pyproject.toml, docker-compose, .env.example, .gitignore
- [x] Step 2a: Config — app/core/config.py (fail-fast validation, secrets hidden)
- [x] Step 2b: Logging — app/core/logging.py (structlog JSON, key-based redaction)
- [x] Step 2c: Errors — app/core/errors.py (AppError + global handlers, one error shape)
- [x] Step 2d: Money helpers — app/utils/money.py (Decimal → paise parser, INR lakh formatter)

## In progress
- (none)

## Next up
- Step 3: Database (session, models, first migration, seeds)

## Open decisions / questions
- RQ worker on Windows → decided: run as a Docker service (added in Phase 2)
- Schema scope → decided: add tables phase by phase
- Log redaction → decided: substring match on normalized keys; `email` is redacted (log user_id instead)
- Money parsing → only empty/whitespace cells are blank; "-" as blank is undecided until real statements are seen

## My TODO(dhruv) tasks
- [x] Nested job-password redaction test (Step 2b, tests/unit/test_logging.py)
- [x] Money parsing + formatting edge-case tests (Step 2d)
- [ ] App version field in /health (Step 4)

## Known issues
- pytest warns that Starlette's TestClient with `httpx` is deprecated (suggests `httpx2`); dependency change pending decision
