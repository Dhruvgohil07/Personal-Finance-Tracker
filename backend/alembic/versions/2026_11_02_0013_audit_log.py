"""audit_log: a record of security-relevant actions

Phase 3 Step 1 (ADR 021, SPEC §5 `audit_log`). The first action recorded
is a user deleting their own profile (`DELETE /me`).

- `user_id` has **no foreign key** to `users`, on purpose: the row that
  records a user's deletion must survive that deletion. A foreign key
  would either block the delete or cascade and remove the record.
- `action` is VARCHAR + CHECK like every enum column (ADR 002).
- No index besides the primary key: nothing reads this table yet, and an
  index costs a write on every insert. The first feature that reads it
  adds the index its query needs.
- Never holds emails or financial values (SPEC §5).

Written by hand; `test_models_match_migrations` checks it against the
models. Migrations 0001-0012 are committed, so this is a new one (rule 7).

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "action",
            sa.Enum(
                "user_deleted",
                name="action",
                native_enum=False,
                create_constraint=True,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("entity", sa.String(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("ip", sa.String(), nullable=True),
        sa.Column("user_agent", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )


def downgrade() -> None:
    # Dropping the table also drops its CHECK constraint.
    op.drop_table("audit_log")
