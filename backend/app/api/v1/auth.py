"""POST /api/v1/auth/register | login | refresh | logout.

These routes only move data between HTTP and app/services/auth.py:
the JSON body in, the access token out in the body, and the refresh token
in and out through a cookie.

Swagger (/docs) note: after logging in, the browser stores the refresh
cookie itself, so "Try it out" on /auth/refresh works without pasting it.
"""

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Cookie, Response

from app.api.deps import DbSession
from app.core.errors import ErrorResponse, UnauthorizedError
from app.core.security import ACCESS_TOKEN_TTL, REFRESH_TOKEN_TTL
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserResponse
from app.services import auth as auth_service
from app.services.auth import INVALID_REFRESH_MESSAGE, AuthTokens

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE_NAME = "refresh_token"
# The browser sends the cookie ONLY to URLs under this path, i.e. to these
# auth routes. Every other API request goes without it, so it is exposed as
# little as possible.
REFRESH_COOKIE_PATH = "/api/v1/auth"

# Shown in /docs for routes that can answer 401.
_UNAUTHORIZED = {HTTPStatus.UNAUTHORIZED: {"model": ErrorResponse}}


def _set_refresh_cookie(response: Response, refresh_token: str) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=refresh_token,
        max_age=int(REFRESH_TOKEN_TTL.total_seconds()),
        path=REFRESH_COOKIE_PATH,
        httponly=True,  # JavaScript can't read it, so an XSS bug can't steal it
        secure=True,  # only sent over HTTPS (browsers make an exception for localhost)
        samesite="strict",  # never sent on requests started by another site (CSRF)
    )


def _token_response(response: Response, tokens: AuthTokens) -> TokenResponse:
    _set_refresh_cookie(response, tokens.refresh_token)
    return TokenResponse(
        access_token=tokens.access_token,
        expires_in=int(ACCESS_TOKEN_TTL.total_seconds()),
    )


@router.post(
    "/register",
    status_code=HTTPStatus.CREATED,
    response_model=UserResponse,
    responses={HTTPStatus.CONFLICT: {"model": ErrorResponse}},
)
def register(body: RegisterRequest, db: DbSession) -> UserResponse:
    user = auth_service.register_user(db, email=body.email, password=body.password, name=body.name)
    return UserResponse.model_validate(user)


@router.post("/login", response_model=TokenResponse, responses=_UNAUTHORIZED)
def login(body: LoginRequest, db: DbSession, response: Response) -> TokenResponse:
    tokens = auth_service.login(db, email=body.email, password=body.password)
    return _token_response(response, tokens)


# `Cookie(alias=...)`: read the value of the cookie called "refresh_token".
# None when the browser didn't send one.
RefreshCookie = Annotated[str | None, Cookie(alias=REFRESH_COOKIE_NAME)]


@router.post("/refresh", response_model=TokenResponse, responses=_UNAUTHORIZED)
def refresh(
    db: DbSession, response: Response, refresh_token: RefreshCookie = None
) -> TokenResponse:
    if refresh_token is None:
        raise UnauthorizedError(INVALID_REFRESH_MESSAGE)
    tokens = auth_service.refresh(db, refresh_token=refresh_token)
    return _token_response(response, tokens)


# No access token needed: logging out must work even after it has expired.
# The refresh cookie identifies the session to end.
@router.post("/logout", status_code=HTTPStatus.NO_CONTENT)
def logout(db: DbSession, response: Response, refresh_token: RefreshCookie = None) -> None:
    auth_service.logout(db, refresh_token=refresh_token)
    # Tell the browser to forget the cookie. The attributes must match the
    # ones it was set with, or the browser treats it as a different cookie.
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=True,
        samesite="strict",
    )
