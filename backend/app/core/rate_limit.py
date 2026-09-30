"""Rate limiting: cap how many requests a client may send in a time window.

Why: without limits, anyone can try thousands of passwords per minute on
/auth/login, or flood the API. SPEC §7.1 sets the limits below.

We use slowapi, a FastAPI wrapper around the `limits` library. It counts
requests in Redis, so the counts are shared by every API process (with an
in-memory counter, each process would allow the full limit on its own).

Three kinds of limits (see ADR 007):

1. Per-route limits, as a decorator on the route, e.g. login:

       @router.post("/login")
       @limiter.limit(AUTH_LIMIT, key_func=client_ip)
       def login(request: Request, ...): ...

   slowapi requires the route to take a `request: Request` parameter.

2. The general limit (GENERAL_LIMIT) for every other route. It is an
   "application limit": ONE counter per client across the whole API,
   checked by RateLimitMiddleware (below) before the route runs.
   Routes with their own decorator skip it (their own limit applies).

3. Exempt routes (`@limiter.exempt`), e.g. /health for uptime monitors.

Every limit has a *key*: who is being counted. See `user_or_ip_key`.
"""

import logging
import math
import time
from http import HTTPStatus

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIASGIMiddleware
from starlette.types import Receive, Scope, Send

from app.core.config import get_settings
from app.core.errors import UnauthorizedError, error_response
from app.core.redis import COMMAND_TIMEOUT_SECONDS, CONNECT_TIMEOUT_SECONDS
from app.core.security import decode_access_token

log = structlog.get_logger()

# Limit strings are parsed by the `limits` library: "<count>/<period>".
AUTH_LIMIT = "5/minute"  # register, login: per IP
UPLOAD_LIMIT = "10/hour"  # statement uploads: per user (used from Step 7)
GENERAL_LIMIT = "100/minute"  # everything else: per user, or per IP if logged out


# --- Keys: who is being counted -------------------------------------------


def client_ip(request: Request) -> str:
    """The IP address of the client that opened the connection.

    We deliberately do NOT read the `X-Forwarded-For` header here: any
    client can send it with a made-up IP and get a fresh counter on every
    request. Behind a real proxy in production, uvicorn's `--proxy-headers`
    option (with `--forwarded-allow-ips` set to the proxy) replaces
    `request.client` with the real client IP, and only trusts the header
    from that proxy.
    """
    host = request.client.host if request.client else "unknown"
    return f"ip:{host}"


def user_or_ip_key(request: Request) -> str:
    """Count logged-in users by user id, everyone else by IP.

    Per user, not per IP: users behind the same IP (an office, a mobile
    carrier's shared IP) don't use up each other's limit.

    Only the token's signature and expiry are checked (no database query),
    because this runs before every request. An invalid or missing token
    falls back to the IP, so sending garbage tokens can't avoid the limit.
    """
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token:
        try:
            return f"user:{decode_access_token(token)}"
        except UnauthorizedError:
            pass
    return client_ip(request)


# --- The limiter ----------------------------------------------------------

# Module-level (not built inside create_app) because the route decorators
# need it when app/api/v1/auth.py is imported.
limiter = Limiter(
    key_func=user_or_ip_key,
    application_limits=[GENERAL_LIMIT],
    storage_uri=get_settings().redis_url,
    # Passed to the Redis client, same timeouts as app/core/redis.py: if
    # Redis stops answering, a request waits at most ~2 s, not forever.
    storage_options={
        "socket_connect_timeout": CONNECT_TIMEOUT_SECONDS,
        "socket_timeout": COMMAND_TIMEOUT_SECONDS,
    },
    # "Fail open": if Redis is down, let the request through unlimited
    # instead of answering 500 to everyone. /health reports Redis as down,
    # so the outage is still noticed. Trade-off explained in ADR 007.
    swallow_errors=True,
)

# slowapi logs "ratelimit 5 per 1 minute (ip:1.2.3.4) exceeded" through
# Python's standard logging, including the key (an IP or user id) and
# bypassing our redaction. We log our own event instead (below).
logging.getLogger("slowapi").disabled = True


# --- 429 response ---------------------------------------------------------


def _retry_after_seconds(request: Request) -> int | None:
    """Seconds until the limit that was hit resets, or None if unknown.

    When a limit is hit, slowapi stores it on `request.state.view_rate_limit`
    as (limit, key parts). We ask the storage when that window ends.
    """
    current = getattr(request.state, "view_rate_limit", None)
    if current is None:
        return None
    limit_item, key_parts = current
    try:
        reset_at, _remaining = limiter.limiter.get_window_stats(limit_item, *key_parts)
    except RedisError:
        return None
    return max(1, math.ceil(reset_at - time.time()))


async def _handle_rate_limit_exceeded(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    # Log which limit was hit on which path, but never the key (IP / user id).
    log.info("rate_limited", path=request.url.path, limit=str(exc.limit.limit))

    retry_after = _retry_after_seconds(request)
    # Retry-After is the standard HTTP header telling clients how long to wait.
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return error_response(
        HTTPStatus.TOO_MANY_REQUESTS,
        "RATE_LIMITED",
        "Too many requests. Please try again later.",
        headers=headers,
    )


class RateLimitMiddleware(SlowAPIASGIMiddleware):
    """slowapi's ASGI middleware, plus a workaround for a bug in slowapi 0.1.10.

    The bug: after checking the limits, slowapi reads
    `request.state.view_rate_limit`. That value is only set when the check
    reached Redis. When Redis is down, the check is skipped (fail open), the
    value is missing, and the request crashes with a 500 instead of being
    let through. Setting a default of None first makes slowapi skip that
    step. The same default also protects the route decorators, which read it
    too.

    `scope["state"]` is the dict behind `request.state` (Starlette builds
    request.state from it), so a default set here is seen by slowapi.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            scope.setdefault("state", {}).setdefault("view_rate_limit", None)
        await super().__call__(scope, receive, send)


def setup_rate_limiting(app: FastAPI) -> None:
    """Attach the limiter to the app. Call BEFORE the other add_middleware().

    Added first = innermost middleware, so a 429 still passes through CORS
    (the browser can read it), SecurityHeaders and RequestLogging.
    """
    # slowapi finds the limiter through app.state (in the middleware and
    # the decorators).
    app.state.limiter = limiter
    # Registered for this exact class: slowapi's middleware looks the
    # handler up by exact type. It also wins over our general
    # StarletteHTTPException handler, because RateLimitExceeded is a subclass.
    app.add_exception_handler(RateLimitExceeded, _handle_rate_limit_exceeded)
    # Based on the ASGI version (not SlowAPIMiddleware) because it can call
    # our async handler above; the other one silently falls back to
    # slowapi's own handler, which returns a different error shape.
    app.add_middleware(RateLimitMiddleware)
