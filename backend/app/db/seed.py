"""Idempotent seed: load system categories and common merchants.

Run it after migrating:

    alembic upgrade head
    python -m app.db.seed

"Idempotent" means running it once or ten times gives the same result.
Each row is an *upsert*, Postgres' `INSERT ... ON CONFLICT ... DO UPDATE`:
- the row doesn't exist yet      -> INSERT it
- it exists (same slug / key)    -> UPDATE its name, icon, etc. to the seed values

The database decides, atomically, using the unique constraint, so there is
no race between "check if it exists" and "insert it".
"""

import structlog
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.seed_data import COMMON_MERCHANTS, SYSTEM_CATEGORIES
from app.db.session import SessionLocal
from app.models import Category, Merchant

log = structlog.get_logger()


def seed_categories(db: Session) -> int:
    """Upsert all system categories (user_id = NULL). Returns the row count."""
    rows = [
        {
            "user_id": None,
            "slug": c.slug,
            "name": c.name,
            "kind": c.kind,
            "icon": c.icon,
            "color": c.color,
        }
        for c in SYSTEM_CATEGORIES
    ]
    stmt = insert(Category).values(rows)
    # `stmt.excluded` is the row we *tried* to insert. On conflict, copy its
    # values onto the existing row. The conflict target is the NULLS NOT
    # DISTINCT constraint, so (NULL, "groceries") matches the existing
    # system "groceries" row.
    stmt = stmt.on_conflict_do_update(
        constraint="uq_categories_user_id_slug",
        set_={
            "name": stmt.excluded.name,
            "kind": stmt.excluded.kind,
            "icon": stmt.excluded.icon,
            "color": stmt.excluded.color,
            # onupdate=now() only fires for ORM updates, so set it explicitly.
            "updated_at": func.now(),
        },
    )
    db.execute(stmt)
    return len(rows)


def seed_merchants(db: Session) -> int:
    """Upsert the common merchant dictionary. Returns the row count."""
    rows = [
        {
            "normalized_key": m.normalized_key,
            "display_name": m.display_name,
            "default_category_slug": m.default_category_slug,
        }
        for m in COMMON_MERCHANTS
    ]
    stmt = insert(Merchant).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_merchants_normalized_key",
        set_={
            "display_name": stmt.excluded.display_name,
            "default_category_slug": stmt.excluded.default_category_slug,
            "updated_at": func.now(),
        },
    )
    db.execute(stmt)
    return len(rows)


def run_seed(db: Session) -> None:
    """Seed everything in ONE transaction: either all rows land, or none do."""
    categories = seed_categories(db)
    merchants = seed_merchants(db)
    db.commit()
    log.info("seed_completed", categories=categories, merchants=merchants)


def main() -> None:
    configure_logging(get_settings().env)
    with SessionLocal() as db:
        run_seed(db)


if __name__ == "__main__":
    main()
