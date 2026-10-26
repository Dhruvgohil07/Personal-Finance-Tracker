"""transactions.merchant_key + (user_id, merchant_key) index

Phase 2 Step 5 (ADR 015): the merchant key read from each narration
("ZOMATO", "MOONCAFE") is now stored. Phase 1 computed it and threw it away,
because `transactions.merchant_id` links to a row of the global merchant
dictionary and most real counterparties (local shops, people) will never be
in it. User rules of type `merchant` (Step 7) and recurring detection
(Phase 4) both need to group a user's rows by the key itself.

Nullable, and existing rows are NOT back-filled: they stay NULL, like their
`channel` (`other`) and `counterparty_vpa` (NULL). The fields can only be
read from the narration, and re-deriving them here would copy narration
rules into a migration that must never change (rule 7). Dev data only:
delete the upload and import it again to fill them (Phase 2 plan).

`channel` and `counterparty_vpa` need no migration: both columns and all
channel values exist since migration 0005.

Written by hand; `test_models_match_migrations` checks it against the
models. Migrations 0001-0010 are committed, so this is a new one (rule 7).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Adding a nullable column with no default is a catalog-only change in
    # Postgres: no table rewrite, no long lock.
    op.add_column("transactions", sa.Column("merchant_key", sa.Text(), nullable=True))
    op.create_index(
        "ix_transactions_user_merchant_key",
        "transactions",
        ["user_id", "merchant_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_transactions_user_merchant_key", table_name="transactions")
    op.drop_column("transactions", "merchant_key")
