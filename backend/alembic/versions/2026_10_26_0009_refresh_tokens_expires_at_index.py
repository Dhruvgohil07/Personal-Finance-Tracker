"""refresh_tokens: index expires_at for the hourly cleanup

The cleanup job (Phase 2 Step 3) deletes refresh-token families whose
every token has expired. Its DELETE starts from `expires_at < :cutoff`, and
without an index that is a scan of every token ever issued, every hour -
one row per refresh, so the table grows by thousands of rows per active
user per month. With the index, only the expired rows are read; the
"no live sibling" check then uses the existing family_id index.

Added in the code review of the Step 3 PR. Migration 0008 is committed, so
this is a new migration (rule 7). The later Phase 2 migrations move up by
one again (plan change log). Written by hand, like 0007;
`test_models_match_migrations` checks it against the model.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        op.f("ix_refresh_tokens_expires_at"), "refresh_tokens", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_refresh_tokens_expires_at"), table_name="refresh_tokens")
