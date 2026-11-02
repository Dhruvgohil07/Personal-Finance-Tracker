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

import hashlib
import hmac
import ipaddress
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
# GET /uploads/{id}, the status a client polls after an upload (Phase 2).
# Its own limit, so polling does not use up the shared GENERAL_LIMIT that
# every other call depends on. The responses ask for one poll every
# UPLOAD_POLL_INTERVAL_SECONDS (Retry-After); 120/min leaves room for a few
# uploads polled at once and for a client that polls a little faster.
UPLOAD_POLL_LIMIT = "120/minute"
# POST /uploads/{id}/password: per user (Phase 2 plan). Each request is one
# guess at a statement password, which is often short and guessable (bank
# PDFs commonly use part of a name plus a date of birth), so it gets the
# same tight limit as login. Named UNLOCK rather than PASSWORD so the
# security linter does not take the constant for a hardcoded password.
UNLOCK_LIMIT = "5/minute"
# DELETE /me: per user. It asks for the account password again (ADR 021),
# so every request is a guess at it; limited like login, so that a stolen
# access token can't be used to try passwords at the general rate.
REAUTH_LIMIT = "5/minute"
GENERAL_LIMIT = "100/minute"  # everything else: per user, or per IP if logged out


# --- Keys: who is being counted -------------------------------------------


# The two headers the Next.js proxy adds to every request it forwards
# (ADR 007, Phase 3 Step 2 amendment): the browser's IP, and an HMAC of that
# IP made with PROXY_SHARED_SECRET. The proxy overwrites any copy the browser
# sent, so a browser can't choose them.
CLIENT_IP_HEADER = "X-Kharcha-Client-IP"
CLIENT_IP_SIGNATURE_HEADER = "X-Kharcha-Client-IP-Signature"


def sign_client_ip(ip: str, secret: str) -> str:
    """HMAC-SHA256 of the IP, as lowercase hex: what the proxy must send.

    HMAC = a hash that only someone holding the secret can compute. The
    backend recomputes it and compares, so a matching signature proves the
    IP was written by our proxy, while the secret itself never travels.
    The proxy computes the same thing with the Web Crypto API.
    """
    return hmac.new(secret.encode(), ip.encode(), hashlib.sha256).hexdigest()


def _signed_client_ip(request: Request) -> str | None:
    """The browser IP the proxy vouched for, or None if there is no valid one.

    None (so the caller falls back to the connection's address) when:
    - no PROXY_SHARED_SECRET is configured (local development);
    - either header is missing (a request that did not come through the
      proxy, e.g. someone calling the API's own URL directly);
    - the signature does not match (forged, or the two sides were given
      different secrets);
    - the signed value is not an IP address (a bug in the proxy).
    """
    secret = get_settings().proxy_shared_secret
    ip = request.headers.get(CLIENT_IP_HEADER)
    signature = request.headers.get(CLIENT_IP_SIGNATURE_HEADER)
    if secret is None or ip is None or signature is None:
        return None

    expected = sign_client_ip(ip, secret.get_secret_value())
    # compare_digest takes the same time whether the first or the last
    # character differs, so the response time can't be used to guess a
    # valid signature one character at a time. Compared as bytes: with str
    # it raises TypeError on non-ASCII input, which a header can contain.
    if not hmac.compare_digest(expected.encode(), signature.encode()):
        # Both headers present but the signature is wrong: a forgery, or
        # (more likely, and worth noticing) the proxy and the API were given
        # different secrets, which silently puts every visitor on one
        # counter. Logged without the IP or the header values (rule 3).
        log.warning("proxy_signature_invalid", path=request.url.path)
        return None

    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        log.warning("proxy_signed_value_not_an_ip", path=request.url.path)
        return None
    # str() normalizes the spelling ("2001:DB8::1" -> "2001:db8::1"), and an
    # IPv4 address written in IPv6 form ("::ffff:198.51.100.1") becomes the
    # plain IPv4 one, so one client always has one spelling.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


# The request.state attribute where client_host() keeps its answer.
_CLIENT_HOST_STATE = "kharcha_client_host"
_NOT_COMPUTED = object()


def client_host(request: Request) -> str | None:
    """The browser's IP address, or None if unknown.

    1. **Behind our Next.js proxy**: the IP from the proxy's signed header.
       Every request reaches the API from the proxy's servers, so the
       connection's address would put all visitors on one counter (5 failed
       logins by anyone would lock out everyone).
    2. **Otherwise**: the address of the connection itself.

    We deliberately do NOT read `X-Forwarded-For`: any client can send it
    with a made-up IP and get a fresh counter on every request. uvicorn's
    `--forwarded-allow-ips` can make that header safe, but only for a proxy
    with known IP addresses, and Vercel's are not a fixed list. Hence the
    signature.

    Also what the audit log records as `ip` (ADR 021), so both always agree
    on who the client is.

    **Worked out once per request.** It is asked several times (the general
    limit's middleware, a route's own limit, the audit log), so the answer
    is kept on `request.state`: the signature is checked once, and a bad one
    is logged once. `request.state` lives in the ASGI scope, which every
    `Request` object built for this request shares.
    """
    cached = getattr(request.state, _CLIENT_HOST_STATE, _NOT_COMPUTED)
    if cached is not _NOT_COMPUTED:
        return cached  # what an earlier call stored: a str or None

    host = _signed_client_ip(request)
    if host is None and request.client is not None:
        host = request.client.host
    setattr(request.state, _CLIENT_HOST_STATE, host)
    return host


# An IPv6 address is counted by its first 64 bits (review of PR 29).
IPV6_PREFIX_LENGTH = 64


def _rate_limit_subject(host: str) -> str:
    """Who a rate limit counts, given the client's address.

    - IPv4: the address itself.
    - IPv6: its /64 network. An ISP gives one household or phone a whole
      /64 (2^64 addresses) and devices pick new addresses inside it freely,
      so counting single addresses would let anyone get a fresh login
      counter per request. A /64 is the usual "one customer" unit.
    - An IPv4 address in IPv6 form ("::ffff:198.51.100.1") counts as the
      IPv4 address; its /64 would be shared by every such client.
    - Not an IP at all (TestClient's "testclient"): used as it is.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        # int(address): the bare 128-bit number, which drops a "%eth0"
        # scope suffix that a network can't carry. IPv6Network (not
        # ip_network) so a small number like ::1 is never read as IPv4.
        return str(ipaddress.IPv6Network((int(address), IPV6_PREFIX_LENGTH), strict=False))
    return str(address)


def client_ip(request: Request) -> str:
    """The rate-limit key for "this client address".

    The audit log keeps the full address (client_host); only the counting
    groups an IPv6 /64 together.
    """
    host = client_host(request)
    return f"ip:{_rate_limit_subject(host) if host is not None else 'unknown'}"


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
