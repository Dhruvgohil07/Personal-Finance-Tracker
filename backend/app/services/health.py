"""Health checks: can we reach Postgres and Redis?

Each check returns True/False instead of raising, so one broken dependency
doesn't stop us from checking (and reporting) the other.
"""

import structlog
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import __version__
from app.schemas.health import CheckStatus, HealthChecks, HealthResponse

log = structlog.get_logger()


def check_database(db: Session) -> bool:
    try:
        # The cheapest possible query: it proves we can get a connection
        # from the pool and that Postgres answers.
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        # Log only the error type: the message can contain host names or
        # connection details.
        log.warning("health_check_failed", check="database", error_type=type(exc).__name__)
        return False
    return True


def check_redis(redis_client: Redis) -> bool:
    try:
        redis_client.ping()
    except RedisError as exc:
        log.warning("health_check_failed", check="redis", error_type=type(exc).__name__)
        return False
    return True


def _status(ok: bool) -> CheckStatus:
    return "ok" if ok else "unavailable"


def get_health(db: Session, redis_client: Redis) -> HealthResponse:
    database_ok = check_database(db)
    redis_ok = check_redis(redis_client)
    return HealthResponse(
        status=_status(database_ok and redis_ok),
        checks=HealthChecks(database=_status(database_ok), redis=_status(redis_ok)),
        version=__version__,
    )
