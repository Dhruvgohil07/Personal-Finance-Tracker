# The FastAPI request lifecycle and dependency injection

How one request, `GET /api/v1/health`, travels through Kharcha, and how
FastAPI's dependency injection (DI) supplies the route with a database
session and a Redis client. Checked against FastAPI 0.141 / Starlette 1.7.

## 1. The big picture

```
 client ──HTTP──> uvicorn ──ASGI──> FastAPI app
                                     │
   ┌─────────────────────────────────┴──────────────────────────────┐
   │ ServerErrorMiddleware    (Starlette) last resort: unhandled     │
   │   │                       exception -> our Exception handler    │
   │   RequestLoggingMiddleware (ours) request id, 1 log line        │
   │     │                                                           │
   │     SecurityHeadersMiddleware (ours) nosniff, DENY, no-store... │
   │       │                                                         │
   │       CORSMiddleware  (FastAPI) only FRONTEND_ORIGIN; answers   │
   │         │                        preflight OPTIONS itself       │
   │         ExceptionMiddleware (Starlette) AppError, validation    │
   │           │                  errors, HTTPException -> handlers  │
   │           Router: match path + method -> route                  │
   │             │                                                   │
   │             Route handler (FastAPI):                            │
   │               1. solve dependencies  (get_health_db, get_redis) │
   │               2. validate inputs     (path/query/body)          │
   │               3. call health()       (in a thread pool)         │
   │               4. validate + serialize output (response_model)   │
   └─────────────────────────────────────────────────────────────────┘
   response travels back OUT through the same layers, in reverse
   after the response is sent: `finally` of get_health_db -> session.close()
```

The two ends are set up in `app/main.py::create_app()`. Starlette always
puts `ServerErrorMiddleware` outermost and `ExceptionMiddleware` innermost;
our `add_middleware()` calls go in between, and **the last one added runs
first** on the way in.

## 2. Step by step

### ① uvicorn: HTTP → ASGI
uvicorn owns the socket. It parses the raw HTTP bytes and calls our app
with the ASGI interface: a `scope` dict (method, path, headers...), plus
`receive` / `send` functions for the body. FastAPI is built on Starlette,
which implements that interface. The app itself never touches sockets.

### ② Middleware, on the way in
Each middleware wraps everything inside it, like layers of an onion. It
can act before calling `call_next(request)` and after it returns.

- **RequestLoggingMiddleware** makes a request id, binds it to structlog's
  contextvars (so every log line of this request carries it) and starts a
  timer (ADR 003).
- **SecurityHeadersMiddleware** does nothing yet; it acts on the way *out*.
- **CORSMiddleware**: for a browser *preflight* (`OPTIONS` with
  `Access-Control-Request-Method`) it answers directly and the route never
  runs. For normal requests it passes through and adds CORS headers to the
  response, but only if the `Origin` is our frontend.
- **ExceptionMiddleware** wraps the router so that known exceptions become
  JSON error responses (step ⑥).

### ③ Routing
The router compares the path and method against every registered route.
`/api/v1/health` comes from `app.include_router(api_router, prefix="/api/v1")`
plus `@router.get("/health")`. No match → 404; path matches but wrong
method → 405. Both raise `HTTPException`, which our handler turns into the
standard error shape.

### ④ Dependency injection
Before calling `health()`, FastAPI reads its signature:

```python
def health(db: HealthDbSession, redis_client: RedisClient, response: Response)
```

`HealthDbSession` is `Annotated[Session, Depends(get_health_db)]`
(`app/api/deps.py`), i.e. "a Session, obtained by calling `get_health_db`".
So FastAPI:

1. calls `get_health_db()`. It is a generator: FastAPI runs it **up to
   `yield`**, takes the yielded session, and remembers the generator so it
   can finish it later;
2. calls `get_redis()`, which returns the process-wide client
   (`@lru_cache`);
3. sees `response: Response` and passes a placeholder response whose status
   code and headers the route may change.

This is **dependency injection**: the route *declares* what it needs; the
framework builds it and *injects* it. The route never creates a session or
knows where one comes from. Three properties make this worth it:

- **Guaranteed cleanup.** Code after `yield` (our `finally: db.close()`)
  always runs, even if the route raised. Nobody can forget to close a
  session.
- **Swappable in tests.** `app.dependency_overrides[get_health_db] = fake`
  makes FastAPI call `fake` instead. The route runs unchanged against a
  fake DB (`tests/unit/test_app.py`) or the test DB
  (`tests/integration/test_health.py`).
- **Composable and cached per request.** A dependency can itself depend on
  others (Phase 1: `get_current_user` will depend on `get_db`). If several
  parts of one request ask for the same dependency, FastAPI calls it
  **once** and shares the result, so a route and `get_current_user` get the
  *same* session. The cache lives for one request only.

### ⑤ Validation, then the route runs
FastAPI validates inputs with Pydantic (this route has none; Phase 1 routes
will have bodies and query parameters). Invalid input never reaches the
route: FastAPI raises `RequestValidationError` → 422.

`health` is a plain `def`, so FastAPI runs it in a **thread pool**
(`run_in_threadpool`). Its DB and Redis calls block, and in a worker thread
they don't stop the event loop from serving other requests. An
`async def` route doing blocking I/O would freeze the whole server while it
waits. Rule of thumb for this project: sync SQLAlchemy → `def` routes.

The route stays thin (rule 6): it calls `get_health(db, redis_client)` in
`app/services/health.py`, and only decides the HTTP status (200 / 503).

### ⑥ Errors
If anything raises, the exception travels **outwards** until a layer
handles it:

| Exception | Handled by | Response |
|---|---|---|
| `AppError` subclasses (ours) | `ExceptionMiddleware` → `_handle_app_error` | its own status + error shape |
| `RequestValidationError` | `ExceptionMiddleware` → `_handle_validation_error` | 422, field names without input values |
| `HTTPException` (404, 405...) | `ExceptionMiddleware` → `_handle_http_exception` | its status + error shape |
| anything else (a bug) | `ServerErrorMiddleware` → `_handle_unexpected_error` | generic 500, logged with traceback |

A handled error becomes a normal response *inside* our middleware, so it
still gets security headers and an `X-Request-ID`. An unhandled one only
becomes a response in `ServerErrorMiddleware`, outside our middleware, so
that 500 has **neither** (see Known issues in `docs/progress.md`).

### ⑦ Response, on the way out
The returned `HealthResponse` is validated against `response_model` and
serialized to JSON. That is the point of response models (rule 11): only
declared fields leave the API, so an ORM object can never leak a column by
accident. The status code set on `response` (503) is applied.

Then outwards through the middleware: CORS adds its headers,
SecurityHeaders adds the security headers, RequestLogging logs
`method, path, status_code, duration_ms` and sets `X-Request-ID`.
uvicorn writes the bytes to the socket.

### ⑧ After the response is sent
FastAPI finishes the `get_health_db` generator: the `finally` block runs
and `db.close()` returns the connection. By default (the `"request"`
scope) this happens **after** the response has been sent. (This timing
has changed between FastAPI versions; the docstring of `Depends` in the
installed version is the source of truth.)
`Depends(..., scope="function")` would close it right after the route
returns instead; we don't need that yet.

## 3. Lifespan: once per process, not per request
`lifespan()` in `app/main.py` runs once at startup (before `yield`) and
once at shutdown (after): there we dispose the engine's connection pool and
close Redis. Engine, pool and Redis client are **per process**; sessions
are **per request**.

## 4. Interview version (30 seconds)
> uvicorn turns HTTP into an ASGI call. The request passes through the
> middleware stack: request-id logging, security headers, CORS, then
> Starlette's exception layer. The router matches the path; FastAPI
> resolves the route's dependencies. `get_db` is a generator dependency, so
> it yields a session and closes it in `finally` after the response, and
> tests replace it through `dependency_overrides`. Inputs are validated by
> Pydantic before the route runs; sync routes run in a thread pool. The
> route calls a service, the return value is validated by the response
> model, and the response goes back out through the middleware. Known
> errors are turned into one JSON error shape by exception handlers.
