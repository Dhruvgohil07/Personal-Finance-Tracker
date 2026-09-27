"""The shared Redis client.

A `redis.Redis` object doesn't hold one connection. It holds a *pool* and
borrows a connection per command, so one client can be shared by the whole
process (like the SQLAlchemy engine in app/db/session.py).

In Phase 2 the RQ queues (app/workers/queue.py) will reuse this client.
"""

from functools import lru_cache

from redis import Redis

from app.core.config import get_settings

# Seconds to wait when connecting / waiting for a reply. Without these,
# a Redis server that never answers would freeze the request forever.
CONNECT_TIMEOUT_SECONDS = 2
COMMAND_TIMEOUT_SECONDS = 2


@lru_cache
def get_redis() -> Redis:
    """Return the process-wide Redis client, created on the first call.

    Creating the client does NOT connect yet; the first command (e.g.
    `ping()`) does. So this never fails at startup, even if Redis is down.
    """
    return Redis.from_url(
        get_settings().redis_url,
        socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
        socket_timeout=COMMAND_TIMEOUT_SECONDS,
    )
