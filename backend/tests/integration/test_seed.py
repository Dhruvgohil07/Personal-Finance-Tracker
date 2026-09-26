"""The seed is idempotent, and the database enforces category uniqueness."""

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.seed import run_seed
from app.db.seed_data import COMMON_MERCHANTS, SYSTEM_CATEGORIES
from app.models import Category, CategoryKind, Merchant

pytestmark = pytest.mark.integration


def count(db: Session, model: type[Category] | type[Merchant]) -> int:
    return db.scalar(select(func.count()).select_from(model))


def test_seed_inserts_all_rows(db: Session) -> None:
    run_seed(db)

    assert count(db, Category) == len(SYSTEM_CATEGORIES)
    assert count(db, Merchant) == len(COMMON_MERCHANTS)


def test_seed_twice_creates_no_duplicates(db: Session) -> None:
    run_seed(db)
    run_seed(db)

    assert count(db, Category) == len(SYSTEM_CATEGORIES)
    assert count(db, Merchant) == len(COMMON_MERCHANTS)


def test_seed_restores_edited_values(db: Session) -> None:
    run_seed(db)
    groceries = db.scalars(select(Category).where(Category.slug == "groceries")).one()
    groceries.name = "Something else"
    db.commit()

    run_seed(db)

    db.refresh(groceries)  # reload from the DB; the seed updated the row with SQL
    assert groceries.name == "Groceries"


def test_system_categories_are_stored_as_expected(db: Session) -> None:
    run_seed(db)
    salary = db.scalars(select(Category).where(Category.slug == "salary")).one()

    assert salary.user_id is None
    assert salary.kind is CategoryKind.INCOME  # converted back to the Python enum


def test_duplicate_system_slug_is_rejected(db: Session) -> None:
    # NULLS NOT DISTINCT: two rows with user_id NULL and the same slug clash.
    db.add(Category(slug="dup", name="First", kind=CategoryKind.EXPENSE))
    db.commit()

    db.add(Category(slug="dup", name="Second", kind=CategoryKind.EXPENSE))
    with pytest.raises(IntegrityError, match="uq_categories_user_id_slug"):
        db.commit()


def test_unknown_kind_is_rejected_by_check_constraint(db: Session) -> None:
    # Raw SQL bypasses the Python enum, so only the CHECK constraint protects us.
    with pytest.raises(IntegrityError, match="ck_categories_kind"):
        db.execute(
            text("INSERT INTO categories (name, slug, kind) VALUES (:name, :slug, :kind)"),
            {"name": "Bad", "slug": "bad", "kind": "gift"},
        )
