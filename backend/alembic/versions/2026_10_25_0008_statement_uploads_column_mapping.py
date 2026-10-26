"""statement_uploads: keep the column mapping for the worker

From Phase 2 Step 2 an RQ worker parses the file after the request has
ended (ADR 013). A generic CSV needs the `column_mapping` the user sent
with it, and an RQ job's only argument is the upload id (SPEC §11), so
the mapping has to be stored where the worker can read it.

A nullable JSONB column: NULL for every upload without a mapping (all
existing rows, and every bank-recognised file). The mapping holds column
header labels and a date pattern, nothing from the rows themselves.

Written by hand (Docker was not running to autogenerate it);
`test_models_match_migrations` checks it against the model.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "statement_uploads",
        sa.Column("column_mapping", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("statement_uploads", "column_mapping")
