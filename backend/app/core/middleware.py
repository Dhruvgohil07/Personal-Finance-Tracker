"""HTTP middleware: code that runs around EVERY request.

A middleware wraps the whole app. For each request it can act before the
route runs (e.g. reject it) and after (e.g. change the response):

    request -> middleware -> route -> middleware -> response

We use it for security headers because they belong on every response, and
adding them in each route would be easy to forget.
"""

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
