"""App exceptions and global exception handlers.

Every error response from the API has the same JSON shape (SPEC §10):

    {"error": {"code": "NOT_FOUND", "message": "Upload not found", "details": null}}

- `code` is a stable, machine-readable string the frontend can switch on.
- `message` is a human-readable sentence, safe to show to the user.
- `details` is optional extra data (e.g. which fields failed validation).

How it works: services raise an `AppError` subclass (e.g. `NotFoundError`).
They never build HTTP responses themselves. FastAPI catches the exception
and calls the matching *exception handler* registered below, which turns it
into the JSON response. Routes and services stay free of response-building
code, and the shape is guaranteed to be the same everywhere.

We handle four kinds of errors:
1. AppError                -> our own errors, with their status and code
2. RequestValidationError  -> invalid input rejected by Pydantic (422)
3. StarletteHTTPException  -> framework errors: unknown URL (404), wrong method (405)
4. Exception               -> any bug we didn't expect (500, generic message)
"""

from http import HTTPStatus
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

log = structlog.get_logger()


# --- Response models ------------------------------------------------------
# Pydantic models for the error body, so the shape is defined in one place
# and can be shown in the OpenAPI docs (/docs) later.


class ErrorInfo(BaseModel):
    code: str
    message: str
    details: Any = None


class ErrorResponse(BaseModel):
    error: ErrorInfo


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(error=ErrorInfo(code=code, message=message, details=details))
    return JSONResponse(status_code=status_code, content=body.model_dump(), headers=headers)


# --- Exceptions -----------------------------------------------------------


class AppError(Exception):
    """Base class for errors we raise on purpose.

    Each subclass sets its HTTP status, code and default message as class
    attributes, so raising one is short: `raise NotFoundError("Upload not found")`.

    The message is sent to the client, so it must never contain sensitive
    data (amounts, narrations, passwords). Put IDs in `details` if needed.
    """

    status_code: int = HTTPStatus.BAD_REQUEST
    code: str = "BAD_REQUEST"
    message: str = "The request could not be processed."

    def __init__(self, message: str | None = None, *, details: Any = None) -> None:
        # Use the given message, or fall back to the class default.
        self.message = message or self.message
        self.details = details
        super().__init__(self.message)


class NotFoundError(AppError):
    # Also used when a row exists but belongs to another user: answering
    # 404 instead of 403 doesn't reveal that the ID exists (IDOR protection).
    status_code = HTTPStatus.NOT_FOUND
    code = "NOT_FOUND"
    message = "The requested resource was not found."


class ConflictError(AppError):
    # e.g. the same statement file was already uploaded.
    status_code = HTTPStatus.CONFLICT
    code = "CONFLICT"
    message = "The request conflicts with the current state."


class UnauthorizedError(AppError):
    # e.g. missing/expired token, or wrong email/password on login.
    status_code = HTTPStatus.UNAUTHORIZED
    code = "UNAUTHORIZED"
    message = "Authentication is required."


# --- Handlers -------------------------------------------------------------
# A handler receives the request and the exception and returns a response.
# FastAPI picks the handler registered for the exception's class (or the
# closest parent class), so the AppError handler also covers NotFoundError.


async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    # Expected errors: log the code only, not the message or details.
    log.info("app_error", code=exc.code, status=exc.status_code, path=request.url.path)
    return error_response(exc.status_code, exc.code, exc.message, exc.details)


async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Pydantic's raw errors include "input": the value the client sent. That
    # could be a password or an amount, so we copy only where the error is,
    # what went wrong and the error type, and never echo the input back.
    details = [
        {
            "field": ".".join(str(part) for part in error["loc"]),  # e.g. "body.email"
            "message": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors()
    ]
    return error_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "VALIDATION_ERROR",
        "The request contains invalid data.",
        details,
    )


async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # Raised by the framework itself (unknown URL -> 404, wrong method -> 405).
    # HTTPStatus(404).name is "NOT_FOUND", which gives us a code for free.
    status = HTTPStatus(exc.status_code)
    message = exc.detail if isinstance(exc.detail, str) else status.phrase
    # Some responses need their headers kept, e.g. 405 sends "Allow: GET".
    return error_response(exc.status_code, status.name, message, headers=exc.headers)


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    # A bug. The client gets a generic message: the real error text could
    # contain internals or user data. The full traceback goes to our logs.
    log.exception("unhandled_error", path=request.url.path, error_type=type(exc).__name__)
    return error_response(
        HTTPStatus.INTERNAL_SERVER_ERROR,
        "INTERNAL_ERROR",
        "Something went wrong on our side. Please try again later.",
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach all handlers to the app (called from main.py in Step 4)."""
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected_error)
