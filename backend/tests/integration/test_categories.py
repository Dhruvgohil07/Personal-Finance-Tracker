"""GET /categories: the seeded defaults plus the caller's own.

`GET /categories` exists so a client can find a `category_id` for
`PATCH /transactions/{id}`. The important property is therefore which rows
it returns: all of the system ones, all of mine, and none of anyone
else's (rule 2).
"""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.seed import run_seed
from app.db.seed_data import SYSTEM_CATEGORIES
from app.models import Category, CategoryKind, User

pytestmark = pytest.mark.integration

CATEGORIES = "/api/v1/categories"

AuthHeadersFactory = Callable[[str], dict[str, str]]


@pytest.fixture
def alice(make_auth_headers: AuthHeadersFactory) -> dict[str, str]:
    return make_auth_headers("alice@example.com")


def user_id_of(db: Session, email: str) -> uuid.UUID:
    return db.scalars(select(User.id).where(User.email == email)).one()


def add_user_category(db: Session, user_id: uuid.UUID, name: str, slug: str) -> Category:
    """A category owned by one user (Phase 2 will add the endpoint for this)."""
    category = Category(user_id=user_id, name=name, slug=slug, kind=CategoryKind.EXPENSE)
    db.add(category)
    db.commit()
    return category


def list_categories(client: TestClient, headers: dict[str, str]) -> list[dict[str, Any]]:
    response = client.get(CATEGORIES, headers=headers)
    assert response.status_code == 200, response.text
    body: list[dict[str, Any]] = response.json()
    return body


def test_the_route_needs_a_token(client: TestClient) -> None:
    response = client.get(CATEGORIES)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_all_system_categories_are_returned(
    client: TestClient, db: Session, alice: dict[str, str]
) -> None:
    run_seed(db)

    items = list_categories(client, alice)

    assert len(items) == len(SYSTEM_CATEGORIES)
    assert all(item["is_system"] for item in items)
    assert {item["slug"] for item in items} == {c.slug for c in SYSTEM_CATEGORIES}


def test_a_category_describes_itself_fully(
    client: TestClient, db: Session, alice: dict[str, str]
) -> None:
    run_seed(db)
    groceries = db.scalars(select(Category).where(Category.slug == "groceries")).one()

    items = list_categories(client, alice)

    assert next(item for item in items if item["slug"] == "groceries") == {
        "id": str(groceries.id),
        "name": "Groceries",
        "slug": "groceries",
        "kind": "expense",
        "parent_id": None,
        "icon": "shopping-basket",
        "color": "#22C55E",
        "is_system": True,
    }
    # user_id is never exposed: it would be null or the caller's own id.
    assert "user_id" not in items[0]


def test_your_own_categories_are_listed_but_not_other_peoples(
    client: TestClient, db: Session, alice: dict[str, str], make_auth_headers: AuthHeadersFactory
) -> None:
    make_auth_headers("bob@example.com")
    mine = add_user_category(
        db, user_id_of(db, "alice@example.com"), "Side project", "side-project"
    )
    add_user_category(db, user_id_of(db, "bob@example.com"), "Bob's budget", "bobs-budget")

    items = list_categories(client, alice)

    # No seed here, so the only rows are the two user categories.
    assert [(item["id"], item["is_system"]) for item in items] == [(str(mine.id), False)]


def test_categories_are_grouped_by_kind(
    client: TestClient, db: Session, alice: dict[str, str]
) -> None:
    """All expenses, then income, then transfers - a dropdown's own order.

    `kind` is a VARCHAR column (ADR 002), so sorting by it is alphabetical,
    which happens to be the order the UI wants. Names are sorted inside
    each group as well, but that order is Postgres' collation (where
    "EMI & Loans" and "Education" compare differently than in Python), so
    only the grouping is asserted here.
    """
    run_seed(db)

    items = list_categories(client, alice)

    kinds = [item["kind"] for item in items]
    # Three all-lowercase words, so Python and Postgres agree on this.
    assert kinds == sorted(kinds)
    assert set(kinds) == {"expense", "income", "transfer"}
