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

## In progress
- (nothing yet)

## Next up
- [ ] Step 4: Accounts CRUD (migration 0003, IDOR test)
- [ ] Step 5: Pure parsing core — parser protocol, registry, normalization, fingerprint (ADR 006)
- [ ] Step 6: File readers (CSV + XLS via xlrd), generic CSV parser, ICICI parser (ADR 004)
- [ ] Step 7: Uploads + import service (migration 0004, idempotent inserts)
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
- Merchant key format → open: confirm against real narrations in Phase 2 (§6.5)
- ICICI statements are legacy `.xls` → decided: support via new dependency `xlrd` (ADR 004 in Step 6)
- Rate limiting → decided: built in Phase 1 (Step 3), not Phase 5
- Phase 1 extra endpoints → decided: only `GET /categories`; `/me`, upload list/delete and
  user categories CRUD deferred
- Refresh token lifetime → decided: 30 days (not in the spec); each refresh issues a new token
- User `name` → required (NOT NULL); `settings` default filled by the ORM, no DB default
- Register → decided: returns 201 + user, no tokens (client logs in next); duplicate → 409 (ADR 005)
- Logout → decided: revokes the whole token family of that session; no access token needed
- Rate limiting → decided: key = user id from a valid JWT, else client IP (never
  X-Forwarded-For); general limit counted across the whole API; /health exempt;
  Redis down = fail open (ADR 007)

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

## Known issues
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
