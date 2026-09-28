"""Database rules for users, refresh tokens and user categories (migration 0002).

These rules are enforced by Postgres itself (constraints, CITEXT, cascades),
so they are tested against the real database, not with mocks.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.seed import run_seed
from app.models import DEFAULT_USER_SETTINGS, Category, CategoryKind, RefreshToken, User

pytestmark = pytest.mark.integration


def _make_user(db: Session, email: str = "dhruv@example.com") -> User:
    # A fake hash is fine here: these tests are about the table, not login.
    user = User(email=email, password_hash="not-a-real-hash", name="Test User")
    db.add(user)
    db.commit()
    return user


def test_new_user_gets_default_settings(db: Session) -> None:
    user = _make_user(db)

    assert user.settings == DEFAULT_USER_SETTINGS
    # Each user has their own copy, not a shared dict.
    assert user.settings is not DEFAULT_USER_SETTINGS


def test_email_is_unique_ignoring_case(db: Session) -> None:
    _make_user(db, "dhruv@example.com")

    db.add(User(email="Dhruv@Example.COM", password_hash="x", name="Someone Else"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_email_lookup_ignores_case(db: Session) -> None:
    user = _make_user(db, "dhruv@example.com")

    found = db.scalar(select(User).where(User.email == "DHRUV@EXAMPLE.COM"))

    assert found is not None
    assert found.id == user.id


def test_deleting_a_user_deletes_their_tokens_and_categories_only(db: Session) -> None:
    run_seed(db)  # system categories (user_id NULL) must survive
    system_count = db.scalar(select(func.count()).select_from(Category))
    user = _make_user(db)
    db.add_all(
        [
            RefreshToken(
                user_id=user.id,
                token_hash="a" * 64,
                family_id=uuid.uuid4(),
                expires_at=datetime.now(UTC) + timedelta(days=30),
            ),
            Category(user_id=user.id, name="Gym", slug="gym", kind=CategoryKind.EXPENSE),
        ]
    )
    db.commit()

    db.delete(user)
    db.commit()

    assert db.scalar(select(func.count()).select_from(RefreshToken)) == 0
    assert db.scalar(select(func.count()).select_from(Category)) == system_count


def test_category_cannot_point_at_a_missing_user(db: Session) -> None:
    db.add(Category(user_id=uuid.uuid4(), name="Ghost", slug="ghost", kind=CategoryKind.EXPENSE))

    with pytest.raises(IntegrityError):
        db.commit()


def test_refresh_token_hash_is_unique(db: Session) -> None:
    user = _make_user(db)
    expires_at = datetime.now(UTC) + timedelta(days=30)
    for _ in range(2):
        db.add(
            RefreshToken(
                user_id=user.id,
                token_hash="same-hash",
                family_id=uuid.uuid4(),
                expires_at=expires_at,
            )
        )

    with pytest.raises(IntegrityError):
        db.commit()
