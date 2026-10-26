"""transactions.statement_seq, statement_uploads.reconciliation_issues

Phase 2 Step 4 (ADR 014):

- `transactions.statement_seq`: the row's position in its upload, oldest
  first. A statement prints no time of day, so without it the
  transactions of one day came back in a random order (the `id` is a
  random UUID), and the balance column could not be followed from row to
  row.
- The keyset index behind `GET /transactions` is rebuilt as
  `(user_id, txn_date DESC, statement_seq DESC, id DESC)`, the new sort key.
- `statement_uploads.reconciliation_issues`: which rows' balances did not
  add up. NULL = not checked.

**Back-filling existing rows.** The column must be NOT NULL, but rows
imported before this step have no recorded order; the file is long gone
(deleted after import, SPEC §7.3). They get a deterministic one -
`row_number()` per upload by `(txn_date, id)`. That is the order the list
already used, so nothing visible changes for them; within a day it is
still arbitrary, as before. Re-importing the statement (delete the upload,
upload again) gives them their real order. Dev data only (Phase 2 plan).

The steps run in this order so no row is ever without a value once the
column is NOT NULL, and the whole migration is one transaction.

Written by hand; `test_models_match_migrations` checks it against the
models. Migrations 0001-0009 are committed, so this is a new one (rule 7).

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("transactions", sa.Column("statement_seq", sa.Integer(), nullable=True))
    # Fixed SQL with no user input, so a plain text statement is safe.
    op.execute(
        """
        UPDATE transactions AS t
        SET statement_seq = numbered.seq
        FROM (
            SELECT id,
                   row_number() OVER (PARTITION BY upload_id ORDER BY txn_date, id) AS seq
            FROM transactions
        ) AS numbered
        WHERE t.id = numbered.id
        """
    )
    op.alter_column("transactions", "statement_seq", nullable=False)
    op.create_check_constraint(
        op.f("ck_transactions_statement_seq_positive"), "transactions", "statement_seq > 0"
    )
    op.create_unique_constraint(
        op.f("uq_transactions_upload_id_statement_seq"),
        "transactions",
        ["upload_id", "statement_seq"],
    )

    op.drop_index("ix_transactions_user_txn_date_id", table_name="transactions")
    op.create_index(
        "ix_transactions_user_txn_date_seq_id",
        "transactions",
        [
            "user_id",
            sa.literal_column("txn_date DESC"),
            sa.literal_column("statement_seq DESC"),
            sa.literal_column("id DESC"),
        ],
        unique=False,
    )

    op.add_column(
        "statement_uploads",
        sa.Column("reconciliation_issues", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("statement_uploads", "reconciliation_issues")
    op.drop_index("ix_transactions_user_txn_date_seq_id", table_name="transactions")
    op.create_index(
        "ix_transactions_user_txn_date_id",
        "transactions",
        ["user_id", sa.literal_column("txn_date DESC"), sa.literal_column("id DESC")],
        unique=False,
    )
    op.drop_constraint(
        op.f("uq_transactions_upload_id_statement_seq"), "transactions", type_="unique"
    )
    op.drop_constraint(
        op.f("ck_transactions_statement_seq_positive"), "transactions", type_="check"
    )
    op.drop_column("transactions", "statement_seq")
