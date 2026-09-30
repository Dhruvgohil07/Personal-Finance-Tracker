# ADR 007 — Rate limiting: slowapi + Redis, keyed by user or IP, fail open

- Status: accepted
- Date: 2026-09-30 (Phase 1, Step 3)
- (ADR 004 and 006 are reserved for Steps 6 and 5 of the Phase 1 plan.)

## Context
SPEC §7.1 asks for: login/register 5/min/IP, uploads 10/hour/user, general
API 100/min/user. Without limits, `/auth/login` allows unlimited password
guessing, and one client can flood the API. Choices to make: where counters
live, who a request is counted against, how "100/min" is counted across
routes, and what happens when Redis (the counter store) is down.

## Decision
1. **slowapi with Redis storage.** Counters live in Redis, so every API
   process shares them. Fixed-window strategy (the default): one counter per
   key per minute/hour.
2. **Keys.**
   - Login and register: client IP (`client_ip`); the caller has no token yet.
   - Everything else: `user:<id>` if the request carries a valid access
     token, otherwise `ip:<address>` (`user_or_ip_key`). Only the JWT
     signature and expiry are checked (no DB query). Invalid tokens fall
     back to the IP, so garbage tokens can't dodge the limit.
   - The IP is `request.client.host`. `X-Forwarded-For` is **not** read
     (anyone can forge it). In production behind a proxy, uvicorn's
     `--proxy-headers --forwarded-allow-ips=<proxy>` sets the real client IP.
3. **General limit = one counter per client across the whole API**
   (slowapi's "application limit"), not one per URL. Per-URL counting would
   give every `/transactions/{id}` its own 100/min. Routes with their own
   decorator (login, register, later uploads) use only their own limit.
4. **`/health` is exempt**: monitors poll it, and it must still answer 503
   when Redis is down.
5. **Fail open.** If Redis is unreachable, requests are let through
   unlimited (`swallow_errors=True`), after the 2 s Redis timeout. /health
   reports Redis as down, so the outage is noticed.
6. **429 response** in our standard error shape, code `RATE_LIMITED`, with a
   `Retry-After` header (seconds until the window resets). We log our own
   `rate_limited` event with path and limit only. slowapi's own logger is
   disabled because it writes the key (IP / user id).
7. **Middleware order**: the rate limiter is the innermost middleware, so a
   429 still gets CORS, security headers and a request ID.
8. **Workaround for a slowapi 0.1.10 bug** (`RateLimitMiddleware`): with
   Redis down, slowapi reads `request.state.view_rate_limit`, which only a
   successful check sets. The request crashes with 500, so fail open
   silently does not work. We set a default of `None` in the ASGI scope
   first. A regression test simulates Redis being down.

## Alternatives rejected
- **In-memory counters**: each process (and each restart) gets its own full
  limit; wrong as soon as there are two workers.
- **Fail closed** (answer 500/503 when Redis is down): the whole API
  would go down with Redis, including reading your own data. Login stays
  somewhat protected anyway: argon2 makes every attempt slow (~tens of ms).
- **slowapi's in-memory fallback**: per-process counters again, and a
  second code path that is rarely exercised.
- **Moving-window / sliding-window strategy**: more accurate at window
  edges (fixed window allows up to 2× the limit around a boundary), but more
  Redis work per request. 10 login attempts in a burst is still acceptable.
- **Our own limiter on top of `limits`**: more code to own; slowapi is what
  the spec names and is widely used with FastAPI.
- **`SlowAPIMiddleware`** (the non-ASGI one): it can't call an async
  exception handler and silently falls back to slowapi's own 429 body.

## Consequences
- Rate-limited routes need a `request: Request` parameter (slowapi reads the
  client from it), and the decorator must sit below `@router.post`.
- Tests: an autouse fixture resets all counters (`limiter.reset()`) before
  each integration test; unit tests turn the limiter off so they don't need
  Redis. Tests and local development share one Redis, so a test run clears
  development counters too (harmless).
- Invalid request bodies (422) on login/register don't count: FastAPI
  validates before the decorator runs. Harmless, since they do no work.
- When upgrading slowapi, check whether the bug in (8) is fixed; the
  regression test will tell if the workaround is still needed.
