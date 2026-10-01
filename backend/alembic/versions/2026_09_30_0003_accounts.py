"""accounts: the user's bank accounts and cards

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # bank_code and account_type are VARCHAR + CHECK enums (ADR 002).
    # ck_accounts_masked_number_4_digits: the database itself refuses
    # anything but exactly 4 digits, so a full account number can never be
    # stored (SPEC §7.3).
    # The unique constraint (user_id, bank_code, masked_number) stops
    # duplicates and doubles as the index for "this user's accounts", so
    # user_id gets no separate index.
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "bank_code",
            sa.Enum(
                "HDFC",
                "SBI",
                "ICICI",
                "GENERIC",
                name="bank_code",
                native_enum=False,
                create_constraint=True,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("nickname", sa.String(length=50), nullable=False),
        sa.Column(
            "account_type",
            sa.Enum(
                "savings",
                "current",
                "credit_card",
                name="account_type",
                native_enum=False,
                create_constraint=True,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("masked_number", sa.String(length=4), nullable=False),
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
        sa.CheckConstraint(
            "masked_number ~ '^[0-9]{4}$'", name=op.f("ck_accounts_masked_number_4_digits")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_accounts_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
        sa.UniqueConstraint(
            "user_id",
            "bank_code",
            "masked_number",
            name=op.f("uq_accounts_user_id_bank_code_masked_number"),
        ),
    )


def downgrade() -> None:
    # Dropping the table also drops its constraints.
    op.drop_table("accounts")
