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
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

# Seconds to wait for Postgres to accept a new connection. Without it,
# psycopg waits forever if the server never answers. Generous, because
# hosted Postgres (Neon) can take a few seconds to wake up.
CONNECT_TIMEOUT_SECONDS = 10

# The health check must answer quickly, even when Postgres is down:
# monitors give up after a few seconds and would never see our 503.
HEALTH_CONNECT_TIMEOUT_SECONDS = 3


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


def build_health_engine(database_url: str) -> Engine:
    """A second engine, used ONLY by the health check.

    Why not reuse `engine`? Its 10 s connect timeout would make /health take
    10 s to report that Postgres is down. A timeout belongs to the engine,
    not to a single query, so a different timeout needs a different engine.

    NullPool = no pool: every check opens a fresh connection and closes it
    afterwards. That's exactly what we want to test ("can we connect right
    now?"), it holds no idle connections, and health checks are rare enough
    that the cost of connecting each time doesn't matter.
    """
    return create_engine(
        database_url,
        poolclass=NullPool,
        connect_args={"connect_timeout": HEALTH_CONNECT_TIMEOUT_SECONDS},
    )


engine = build_engine(get_settings().database_url)
health_engine = build_health_engine(get_settings().database_url)

# expire_on_commit=False: after commit(), objects keep their loaded values.
# Otherwise, reading `category.name` after commit would trigger a new SELECT,
# which fails if the session has already been closed.
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
HealthSessionLocal = sessionmaker(bind=health_engine)


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


def get_health_db() -> Iterator[Session]:
    """Like get_db, but the session uses the fast-failing health engine."""
    db = HealthSessionLocal()
    try:
        yield db
    finally:
        db.close()
