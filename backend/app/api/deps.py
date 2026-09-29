"""Shared FastAPI dependencies, as ready-to-use type aliases.

`Annotated[Session, Depends(get_db)]` means: "this parameter is a Session,
and FastAPI should get it by calling get_db()". Giving that a short name
lets every route write just:

    def my_route(db: DbSession, user: CurrentUser) -> ...:
"""

from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis import Redis
from sqlalchemy.orm import Session

from app.core.errors import UnauthorizedError
from app.core.redis import get_redis
from app.core.security import decode_access_token
from app.db.session import get_db, get_health_db
from app.models import User

DbSession = Annotated[Session, Depends(get_db)]
# Only for GET /health: fails after 3 s instead of 10 s (see app/db/session.py).
HealthDbSession = Annotated[Session, Depends(get_health_db)]
RedisClient = Annotated[Redis, Depends(get_redis)]

# HTTPBearer reads the `Authorization: Bearer <token>` header. Using it (not
# reading the header by hand) also adds the "Authorize" button to /docs.
# auto_error=False: if the header is missing, give us None instead of raising
# FastAPI's own 403, so we can answer 401 in our standard error shape.
_bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> User:
    """The logged-in user, or a 401 if the access token is missing or invalid.

    Every protected route depends on this. Services then receive `user.id`
    and filter every query by it (rule 2: every query is scoped by user_id).
    """
    if credentials is None:
        raise UnauthorizedError()

    user_id = decode_access_token(credentials.credentials)

    # The token is valid, but the user may have been deleted since it was
    # issued (it lives up to 15 minutes), so we still check the database.
    user = db.get(User, user_id)
    if user is None:
        raise UnauthorizedError("Invalid or expired token.")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
