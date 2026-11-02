"""transactions(category_id) index

Phase 3 Step 1, review of PR 28. `transactions.category_id` is a foreign key
with `ON DELETE SET NULL`. When a category row is deleted, Postgres runs

    UPDATE transactions SET category_id = NULL WHERE category_id = $1

once per deleted category. Postgres does not index foreign-key columns on
its own, and the existing `ix_transactions_user_category_date` starts with
`user_id`, so it cannot serve a lookup by category alone: each deleted
category was a sequential scan of every user's transactions. `DELETE /me`
deletes all of a user's own categories in one go (ADR 021), and Phase 4's
custom categories will make that common.

A plain b-tree on `category_id`. NULLs are indexed too; a partial index
(`WHERE category_id IS NOT NULL`) would be smaller, but the plain one is
the standard foreign-key index and needs no explanation of when the
planner may use it.

Written by hand; `test_models_match_migrations` checks it against the
models. Migrations 0001-0013 are committed, so this is a new one (rule 7).

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_transactions_category_id", "transactions", ["category_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_transactions_category_id", table_name="transactions")
