"""GET /api/v1/health: is the API able to serve requests?

Used by us, by CI, and later by the hosting platform to decide whether the
app is up. Returns 200 when Postgres and Redis are both reachable, else 503.
"""

from http import HTTPStatus

from fastapi import APIRouter, Response

from app.api.deps import DbSession, RedisClient
from app.schemas.health import HealthResponse
from app.services.health import get_health

router = APIRouter(tags=["health"])


# `def`, not `async def`: the DB and Redis calls are blocking. FastAPI runs
# plain `def` routes in a thread pool, so a slow check doesn't block other
# requests. (An `async def` route doing blocking calls would.)
#
# `responses=` documents the 503 case in /docs; its body has the same shape.
@router.get(
    "/health",
    response_model=HealthResponse,
    responses={HTTPStatus.SERVICE_UNAVAILABLE: {"model": HealthResponse}},
)
def health(db: DbSession, redis_client: RedisClient, response: Response) -> HealthResponse:
    result = get_health(db, redis_client)
    if result.status != "ok":
        # Declaring `response: Response` gives us the response FastAPI will
        # send, so we can change its status code but still return our model.
        response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
    return result
