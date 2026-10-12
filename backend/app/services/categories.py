"""Categories business logic: list them, and check that one is usable.

A category row is either a **system** category (`user_id IS NULL`, created
by the seed and shared by everyone) or a **user** category (`user_id` = its
owner). Both functions here therefore ask the same question - "which
categories may THIS user use?" - and the answer is always

    user_id IS NULL  OR  user_id = <the caller>

That is still rule 2 (every query is scoped by user_id): the `IS NULL` half
is not a hole, it is the shared dictionary, and the other half can never
widen to someone else's rows. A category belonging to another user is
invisible, which is why `get_category` answers 404 for it rather than 403
(the same IDOR reasoning as `accounts_service.get_account`).

User categories cannot be created through the API in Phase 1, so today
every row is a system one - but the queries are written for both, so adding
`POST /categories` in Phase 2 needs no change here.
"""

import uuid

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models import Category

CATEGORY_NOT_FOUND_MESSAGE = "Category not found."


def _visible_to(user_id: uuid.UUID) -> ColumnElement[bool]:
    """The WHERE clause for "a category this user may use"."""
    # or_() builds `a OR b`; `.is_(None)` renders `IS NULL` (a plain
    # `== None` would be the SQL `= NULL`, which is never true).
    return or_(Category.user_id.is_(None), Category.user_id == user_id)


def list_categories(db: Session, user_id: uuid.UUID) -> list[Category]:
    """Every category the user can choose from: the defaults plus their own.

    Ordered by kind and then name, so the list is grouped the way a
    dropdown wants it (all expenses, then income, then transfers) and is
    stable between calls. `kind` is a VARCHAR column (ADR 002), so that
    ordering is alphabetical - expense, income, transfer - which happens to
    be the order the UI wants anyway.

    No pagination: there are 25 system categories and a user will add a
    handful, so the whole list is one small response the client can keep.
    """
    stmt = select(Category).where(_visible_to(user_id)).order_by(Category.kind, Category.name)
    return list(db.scalars(stmt))


def get_category(db: Session, user_id: uuid.UUID, category_id: uuid.UUID) -> Category:
    """One category the user may use, or `NotFoundError` (404).

    Used by the transactions service before it writes `category_id` on a
    row: without this check a user could move their transaction into
    another user's private category, which would then leak that category's
    id - and later its name - back to them through `GET /transactions`.
    """
    stmt = select(Category).where(Category.id == category_id, _visible_to(user_id))
    category = db.scalar(stmt)
    if category is None:
        raise NotFoundError(CATEGORY_NOT_FOUND_MESSAGE)
    return category
