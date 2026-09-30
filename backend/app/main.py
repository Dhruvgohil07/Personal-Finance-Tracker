"""The FastAPI application: where all the pieces are wired together.

Run locally (from backend/):

    uvicorn app.main:app --reload

`app.main:app` means "in module app/main.py, use the variable `app`".
Interactive API docs are then at http://127.0.0.1:8000/docs
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.v1 import api_router
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import (
    REQUEST_ID_HEADER,
    RequestLoggingMiddleware,
    SecurityHeadersMiddleware,
)
from app.core.rate_limit import setup_rate_limiting
from app.core.redis import get_redis
from app.db.session import engine

API_V1_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Code that runs once at startup (before `yield`) and once at shutdown (after).

    Nothing is needed at startup yet. At shutdown we close the pooled
    connections cleanly instead of letting them be cut off.
    """
    yield
    engine.dispose()
    get_redis().close()


def create_app() -> FastAPI:
    """Build and configure the app (the "app factory" pattern).

    Building the app inside a function, instead of at module level, means
    tests can create a fresh app whenever they need one.
    """
    settings = get_settings()
    configure_logging(settings.env)

    app = FastAPI(title="Kharcha API", version=__version__, lifespan=lifespan)

    register_exception_handlers(app)

    # Middleware order: each add_middleware() call wraps AROUND everything
    # added before it, so the LAST one added runs FIRST on a request.
    #
    #   request -> RequestLogging -> SecurityHeaders -> CORS -> RateLimit -> route
    #
    # RateLimit is innermost, so its 429 answers still get CORS headers
    # (a browser can read them), security headers and a log line.
    # CORS answers browser "preflight" requests (OPTIONS) itself, without
    # calling the route, so preflights are never rate limited.
    # SecurityHeaders sits outside CORS, so those answers get the security
    # headers too: every response passes through it.
    # RequestLogging is outermost, so it times and logs everything,
    # including requests that CORS rejects.
    setup_rate_limiting(app)
    app.add_middleware(
        CORSMiddleware,
        # Only our own frontend may call the API from a browser. Never "*".
        allow_origins=[settings.frontend_origin],
        # Lets the browser send the refresh-token cookie (Phase 1). This is
        # also why "*" would be unsafe: any site could use the user's cookie.
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
        # Browsers hide response headers from JavaScript unless listed here.
        # The frontend needs to read X-Request-ID to show it in error messages.
        expose_headers=[REQUEST_ID_HEADER],
    )
    app.add_middleware(SecurityHeadersMiddleware, enable_hsts=settings.env == "production")
    app.add_middleware(RequestLoggingMiddleware)

    app.include_router(api_router, prefix=API_V1_PREFIX)
    return app


app = create_app()
