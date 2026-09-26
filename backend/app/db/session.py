"""Database engine, session factory and the `get_db` FastAPI dependency.

Three objects, from lowest to highest level:

- engine        : owns a *pool* of open connections to Postgres. Created once
                  per process, because opening a connection is slow.
- SessionLocal  : a factory. Calling SessionLocal() gives a new Session.
- Session       : one "unit of work". It borrows a connection from the pool,
                  tracks the objects you load/add, and sends the SQL on
                  commit(). One session per request (or per background job).
"""

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

# Seconds to wait for Postgres to accept a new connection. Without it,
# psycopg waits forever if the server never answers.
CONNECT_TIMEOUT_SECONDS = 10


def build_engine(database_url: str) -> Engine:
    return create_engine(
        database_url,
        # Before handing out a pooled connection, check it's still alive
        # (hosted Postgres like Neon closes idle connections).
        pool_pre_ping=True,
        # connect_args are passed straight to psycopg.connect(). A hung
        # connection attempt now fails with an error instead of freezing.
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )


engine = build_engine(get_settings().database_url)

# expire_on_commit=False: after commit(), objects keep their loaded values.
# Otherwise, reading `category.name` after commit would trigger a new SELECT,
# which fails if the session has already been closed.
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed afterwards.

    Used in a route like:  def list_things(db: Session = Depends(get_db)): ...

    Because this is a generator, FastAPI runs the code before `yield` when
    the request starts, hands the session to the route, and runs the
    `finally` block after the response is sent, even if the route raised.
    Services decide when to commit; closing without commit rolls back.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
