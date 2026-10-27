"""category_rules: the user's own categorization rules

Phase 2 Step 7 (ADR 017, SPEC §5 `category_rules`, §8.1). A rule says
"rows that match this pattern belong in this category"; the categorizer's
user-rule handler (ADR 016) tries them before anything built in.

- `match_type` is VARCHAR + CHECK like every enum column (ADR 002).
- unique (user_id, match_type, pattern): one rule per thing matched. It
  starts with user_id, so it also serves "this user's rules".
- `category_id` CASCADE: a rule for a deleted category goes with it.
- `created_from_transaction_id` SET NULL: the rule a user taught outlives
  the upload it was learned from.
- Both of those foreign keys get an index, so a cascading delete finds the
  rules pointing at a row without scanning the table once per row.

Written by hand; `test_models_match_migrations` checks it against the
models. Migrations 0001-0011 are committed, so this is a new one (rule 7).

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "category_rules",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "match_type",
            sa.Enum(
                "merchant",
                "vpa",
                "contains",
                name="match_type",
                native_enum=False,
                create_constraint=True,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("pattern", sa.Text(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("priority", sa.Integer(), server_default="100", nullable=False),
        sa.Column("created_from_transaction_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_category_rules_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name=op.f("fk_category_rules_category_id_categories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_from_transaction_id"],
            ["transactions.id"],
            name=op.f("fk_category_rules_created_from_transaction_id_transactions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_category_rules")),
        sa.UniqueConstraint(
            "user_id",
            "match_type",
            "pattern",
            name=op.f("uq_category_rules_user_id_match_type_pattern"),
        ),
    )
    op.create_index("ix_category_rules_category_id", "category_rules", ["category_id"])
    op.create_index(
        "ix_category_rules_created_from_transaction_id",
        "category_rules",
        ["created_from_transaction_id"],
    )


def downgrade() -> None:
    # Dropping the table also drops its constraints and indexes.
    op.drop_table("category_rules")
