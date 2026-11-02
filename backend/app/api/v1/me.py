"""GET /api/v1/me and DELETE /api/v1/me: the logged-in user's own profile.

"Me" routes take no user id in the URL at all: the user is always the one
the access token belongs to, so there is no id another user could swap in
(no IDOR possible by construction).
"""

from http import HTTPStatus

from fastapi import APIRouter, Request, Response

from app.api.deps import CurrentUser, DbSession
from app.api.v1.auth import clear_refresh_cookie
from app.core.errors import ErrorResponse
from app.core.rate_limit import REAUTH_LIMIT, client_host, limiter
from app.schemas.me import DeleteMeRequest, MeResponse
from app.services import me as me_service
from app.services.me import RequestOrigin

router = APIRouter(prefix="/me", tags=["me"])


@router.get("", response_model=MeResponse)
def get_me(user: CurrentUser) -> MeResponse:
    return MeResponse.model_validate(user)


# DELETE with a JSON body (the password). HTTP allows a body on DELETE, and
# fetch() and the Next.js proxy pass it on; SPEC §10 names this route
# DELETE /me, so it is kept rather than renamed to a POST.
# 5 per minute per user: each request is a guess at the password
# (REAUTH_LIMIT). The decorator must sit below @router.delete.
@router.delete(
    "",
    status_code=HTTPStatus.NO_CONTENT,
    responses={
        HTTPStatus.BAD_REQUEST: {"model": ErrorResponse},
        HTTPStatus.TOO_MANY_REQUESTS: {"model": ErrorResponse},
    },
)
@limiter.limit(REAUTH_LIMIT)
def delete_me(
    request: Request,
    response: Response,
    user: CurrentUser,
    db: DbSession,
    body: DeleteMeRequest,
) -> None:
    origin = RequestOrigin(ip=client_host(request), user_agent=request.headers.get("User-Agent"))
    me_service.delete_user(db, user, password=body.password, origin=origin)
    # Only reached when the delete succeeded: a wrong password raises above,
    # and the browser keeps its (still valid) session.
    clear_refresh_cookie(response)
