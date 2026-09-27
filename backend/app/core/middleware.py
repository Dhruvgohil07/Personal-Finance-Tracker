"""HTTP middleware: code that runs around EVERY request.

A middleware wraps the whole app. For each request it can act before the
route runs (e.g. reject it) and after (e.g. change the response):

    request -> middleware -> route -> middleware -> response

We use it for things that belong to every request, where doing it in each
route would be easy to forget: security headers and request logging.
"""

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

# Headers that tell the BROWSER to be stricter. They cost nothing for API
# clients like curl, and close whole classes of attacks in browsers.
BASE_SECURITY_HEADERS: dict[str, str] = {
    # Don't guess ("sniff") the content type: a JSON response must never be
    # run as HTML or JavaScript.
    "X-Content-Type-Options": "nosniff",
    # Never show our responses inside an <iframe> on another site
    # (prevents "clickjacking").
    "X-Frame-Options": "DENY",
    # When a user follows a link, don't tell the other site which URL they
    # came from (our URLs can contain IDs and filters).
    "Referrer-Policy": "no-referrer",
    # Our pages never need the camera, microphone or location.
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    # Responses contain financial data: browsers and proxies must not store
    # them on disk. A route can still set its own Cache-Control if needed.
    "Cache-Control": "no-store",
}

# HSTS: "only ever talk to this site over HTTPS, for the next year".
# Only sent in production, because local development runs on plain HTTP.
HSTS_HEADER = ("Strict-Transport-Security", "max-age=31536000; includeSubDomains")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, enable_hsts: bool = False) -> None:
        super().__init__(app)
        self.headers = dict(BASE_SECURITY_HEADERS)
        if enable_hsts:
            name, value = HSTS_HEADER
            self.headers[name] = value

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # `call_next` runs the rest of the app (other middleware + the route)
        # and gives back its response, which we can then modify.
        response = await call_next(request)
        for name, value in self.headers.items():
            # setdefault: only add the header if the route didn't set it.
            response.headers.setdefault(name, value)
        return response


# --- Request logging ------------------------------------------------------

REQUEST_ID_HEADER = "X-Request-ID"

log = structlog.get_logger()


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log one line per request, and tag every log line with a request ID.

    This replaces uvicorn's access log (disabled in app/core/logging.py),
    which writes the full URL including the query string, e.g.
    `/transactions?min_amount=5000&q=zomato`, and doesn't go through our
    redaction. Here we log `request.url.path` only: the path WITHOUT the
    query string. We never log headers or bodies either.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # A random ID for this request. We always make our own instead of
        # trusting one sent by the client, which could contain anything.
        request_id = uuid.uuid4().hex

        # contextvars = "global variables, but separate for each request".
        # Binding request_id here makes the `merge_contextvars` processor
        # (see app/core/logging.py) add it to EVERY log line written while
        # this request is handled, including logs from services. So all
        # lines of one request can be found by searching for its ID.
        # clear first: never carry over values from a previous request.
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # An unhandled bug: the global handler (app/core/errors.py) turns
            # it into a 500 response further out. We still log the request,
            # then let the exception continue on its way.
            self._log(request, status_code=500, start=start)
            raise

        self._log(request, status_code=response.status_code, start=start)
        # Returned to the client too: a user reporting a problem can give us
        # this ID, and we can find exactly their request in the logs.
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    @staticmethod
    def _log(request: Request, *, status_code: int, start: float) -> None:
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        log.info(
            "request",
            method=request.method,
            path=request.url.path,  # no query string, on purpose
            status_code=status_code,
            duration_ms=duration_ms,
        )
