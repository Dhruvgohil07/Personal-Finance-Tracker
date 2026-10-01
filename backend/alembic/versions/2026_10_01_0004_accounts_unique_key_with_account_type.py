"""accounts: add account_type to the unique key

A savings account and a credit card at the same bank can end in the same
4 digits; the old key (user_id, bank_code, masked_number) wrongly treated
them as one account and answered 409.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Safe on existing data: every row unique under the old (narrower) key is
    # still unique under the new, wider one. Both steps run in one
    # transaction, so there is no moment without a unique constraint.
    op.drop_constraint(
        op.f("uq_accounts_user_id_bank_code_masked_number"), "accounts", type_="unique"
    )
    op.create_unique_constraint(
        op.f("uq_accounts_user_id_bank_code_account_type_masked_number"),
        "accounts",
        ["user_id", "bank_code", "account_type", "masked_number"],
    )


def downgrade() -> None:
    # Note: this fails if a user already has two accounts that differ only in
    # account_type (e.g. HDFC savings 4821 + HDFC credit card 4821); one of
    # them would have to be deleted first.
    op.drop_constraint(
        op.f("uq_accounts_user_id_bank_code_account_type_masked_number"), "accounts", type_="unique"
    )
    op.create_unique_constraint(
        op.f("uq_accounts_user_id_bank_code_masked_number"),
        "accounts",
        ["user_id", "bank_code", "masked_number"],
    )
