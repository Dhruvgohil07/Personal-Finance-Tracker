"""Shared FastAPI dependencies, as ready-to-use type aliases.

`Annotated[Session, Depends(get_db)]` means: "this parameter is a Session,
and FastAPI should get it by calling get_db()". Giving that a short name
lets every route write just:

    def my_route(db: DbSession) -> ...:

In Phase 1, `get_current_user` (and a `CurrentUser` alias) will live here too.
"""

from typing import Annotated

from fastapi import Depends
from redis import Redis
from sqlalchemy.orm import Session

from app.core.redis import get_redis
from app.db.session import get_db

DbSession = Annotated[Session, Depends(get_db)]
RedisClient = Annotated[Redis, Depends(get_redis)]
