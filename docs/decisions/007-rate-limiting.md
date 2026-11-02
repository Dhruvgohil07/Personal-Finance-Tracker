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

## Amendment — Phase 2 Step 2 (2026-10-26)
`GET /uploads/{id}` is polled by a client while the worker imports an upload
(ADR 013). Under the general limit, those polls would use up the one shared
counter that every other call depends on. So the route has its own decorator
limit, `UPLOAD_POLL_LIMIT = "120/minute"` per user. As decision 3 says, a
decorated route uses only its own limit, and its counter is shared by all
upload ids of that user. Unfinished uploads also send `Retry-After: 2`, so a
well-behaved client polls about 30 times a minute.

## Amendment — Phase 3 Step 1 (2026-11-02)
`DELETE /me` asks for the account password again (ADR 021), so every request is
a guess at it. It has its own decorator limit, `REAUTH_LIMIT = "5/minute"` per
user, like login. Under the general limit alone, a stolen access token could try
about 1,500 passwords in its 15 minutes. `client_ip()` now builds its key from a
new `client_host()`, which the audit log also uses for its `ip` column.

## Amendment — Phase 3 Step 2 (2026-11-02): the client IP behind the proxy
**Context.** From Phase 3 on, the browser reaches the API only through the Next.js
proxy (Phase 3 plan, hard constraint 2). Every request then arrives from the proxy's
servers, so `request.client` is the same few addresses for every visitor. Five failed
logins by anyone would lock out everyone, and `audit_log.ip` (ADR 021) would always
record the proxy. `X-Forwarded-For` can't be trusted: any client can send it, and
uvicorn's `--forwarded-allow-ips` only helps for a proxy with known IPs, which
Vercel's are not.

**Decision (chosen with Dhruv: HMAC of the IP).** The proxy adds two headers to every
request it forwards, overwriting any the browser sent:

| Header | Value |
|---|---|
| `X-Kharcha-Client-IP` | the browser's IP |
| `X-Kharcha-Client-IP-Signature` | HMAC-SHA256 of that IP with `PROXY_SHARED_SECRET`, lowercase hex |

`client_host()` uses the IP only when the signature matches (compared in constant
time, as bytes) and the value is a valid IP address (normalized, so one IPv6 address
has one spelling). In every other case it falls back to the connection's address:
no secret configured (local development), a header missing (someone calling the API's
own URL directly), a wrong signature, or a value that is not an IP. A forged header
therefore gets an attacker nothing: they are counted as themselves.
`PROXY_SHARED_SECRET` is optional and, when set, at least 32 characters. Both the rate
limits and the audit log read `client_host()`, so both follow.

`tests/unit/test_rate_limit_keys.py` pins one test vector (also checked with OpenSSL);
the proxy's JavaScript (Step 3/4) must produce the same value.

**Alternatives rejected.**
- *Sending the secret itself in a header.* Simplest, but the secret would travel in
  every request, and any future header logging would leak it.
- *HMAC of the IP plus a timestamp, valid for 60 s.* Stops replaying a captured
  header pair, but needs more code on both sides and depends on the Vercel and Render
  clocks agreeing. A replayed pair only lets someone use that one IP's counter, and
  the traffic between proxy and API is HTTPS.
- *Rejecting requests without a valid signature (403).* Would lock out local
  development and the `/docs` page, which have no proxy in front of them.

**Consequences.**
- Production must set the SAME `PROXY_SHARED_SECRET` on Render and Vercel (Step 11).
  If they differ, every visitor falls back to the proxy's address and all share one
  counter again; the review fixes below make that visible in the logs.
- Closes the "rate limits key on the socket IP behind a proxy" known issue, once the
  proxy sends the headers (Step 3/4).

**Review of PR 29 (2026-11-02).** Five changes:
1. **IPv6 is counted per /64.** An ISP gives a household or phone a whole /64 and
   devices pick new addresses inside it freely, so counting single addresses gave a
   fresh login counter per request. `client_ip()` (the rate-limit key) now uses the
   address's /64 network; IPv4 stays the single address. An IPv4 address written in
   IPv6 form (`::ffff:a.b.c.d`) counts as the IPv4 one. The audit log still records
   the exact address (`client_host()`).
2. **`ENV=production` refuses to start without `PROXY_SHARED_SECRET`**, instead of
   silently putting every visitor on one counter.
3. **`PROXY_SHARED_SECRET` must differ from both JWT secrets.** It is also stored on
   Vercel; reusing a JWT secret there would let a leak of the frontend's settings sign
   access tokens.
4. **A bad signature is logged** (`proxy_signature_invalid`, and
   `proxy_signed_value_not_an_ip`) with the path only, never the IP or the header
   values. `client_host()` keeps its answer on `request.state`, so the signature is
   checked, and a failure logged, once per request although several key functions
   ask for it. A request with no proxy headers logs nothing (calling the API
   directly, e.g. `/docs`, is normal).
5. The unit test for "no secret configured" now removes the secret itself instead of
   assuming the developer's `.env` has none.
