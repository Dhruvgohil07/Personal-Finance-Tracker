"""Spending/income categories (SPEC §5 `categories`).

Two kinds of rows live in this one table:
- system categories  : user_id IS NULL, created by the seed, shared by everyone
- user categories    : user_id = the owner (created through the API later)
"""

import uuid
from enum import StrEnum

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum_column


class CategoryKind(StrEnum):
    EXPENSE = "expense"
    INCOME = "income"
    # Money moving between your own accounts: excluded from spending/income totals.
    TRANSFER = "transfer"


class Category(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "categories"
    __table_args__ = (
        # NULLS NOT DISTINCT (Postgres 15+): treat NULL user_id as a normal
        # value, so there can only be ONE system category per slug. Without it,
        # Postgres considers NULL != NULL and would allow duplicate system rows.
        UniqueConstraint("user_id", "slug", postgresql_nulls_not_distinct=True),
    )

    # NULL = system category. The FOREIGN KEY was added in migration 0002,
    # together with the users table (see ADR 002). Deleting a user deletes
    # their own categories; system categories (NULL) are never affected.
    # No separate index: the unique constraint (user_id, slug) starts with
    # user_id, so Postgres can use it for "WHERE user_id = ..." too.
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str]
    # URL/code-friendly id, e.g. "food-dining". Code refers to categories by
    # slug (merchants, keyword rules, LLM output), never by UUID.
    slug: Mapped[str]
    # Optional parent for sub-categories. If the parent is deleted, the child
    # becomes a top-level category instead of being deleted with it.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL")
    )
    # Stored as VARCHAR + a CHECK constraint (not a native Postgres ENUM), so
    # adding a value later is a simple migration. See ADR 002.
    kind: Mapped[CategoryKind] = mapped_column(str_enum_column(CategoryKind, "kind"))
    # Icon name (from the frontend's icon set) and hex colour, for the UI.
    icon: Mapped[str | None]
    color: Mapped[str | None]

    def __repr__(self) -> str:
        return f"Category(slug={self.slug!r}, user_id={self.user_id})"
