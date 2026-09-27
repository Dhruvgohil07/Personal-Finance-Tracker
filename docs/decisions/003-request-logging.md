# ADR 003 — Request logging through structlog, not uvicorn's access log

- Status: accepted
- Date: 2026-09-27 (Phase 0, after Step 4)

## Context
uvicorn writes its own access log line for every request, e.g.
`"GET /api/v1/transactions?min_amount=5000&q=zomato HTTP/1.1" 200`.
That line uses Python's standard `logging`, not structlog, so our redaction
processor never sees it, and the query string can hold amounts and search
terms. Rule 3 (never log amounts or narrations) would be broken as soon as
the Phase 1 filter endpoints exist.

## Decision
1. **Disable `uvicorn.access`** in `configure_logging()`
   (`logging.getLogger("uvicorn.access").disabled = True`). uvicorn imports
   the app after configuring its own logging, so this setting wins, also
   with `--reload`.
2. **`RequestLoggingMiddleware`** logs one structlog line per request:
   `method`, `path` (without the query string), `status_code`,
   `duration_ms`. Never headers, bodies or query strings. It is the
   outermost middleware, so it also logs CORS-rejected requests.
3. **Request ID**: a fresh `uuid4().hex` per request, bound with structlog
   contextvars so every log line of that request carries it, and returned
   in the `X-Request-ID` response header (exposed to the frontend via
   CORS `expose_headers`). A client-sent `X-Request-ID` is ignored.

## Alternatives rejected
- *`uvicorn --no-access-log` flag*: depends on every start command (local,
  Docker, hosting) remembering the flag; the code-level setting can't be
  forgotten.
- *Redact the query string inside uvicorn's log format*: needs a custom
  stdlib logging filter parsing uvicorn's message format; more code, and
  still outside our structlog pipeline.
- *Log the query string with redacted values*: parameter names alone can
  be revealing, and we have no need for them in logs.
- *Accept the client's `X-Request-ID`*: useful behind a trusted proxy, but
  an untrusted client could put arbitrary text into our logs. Can be added
  later with strict format validation if a proxy needs it.

## Consequences
- Logs never show query parameters; debugging a filter bug needs a
  reproduction, not the logs.
- A request that crashes with an unhandled exception is logged as 500, but
  its response has no `X-Request-ID` header (Starlette's outermost error
  layer builds that response, outside our middleware).
- Other standard-library loggers (`uvicorn.error`, SQLAlchemy) still bypass
  structlog. They log startup messages and tracebacks, not request data.
