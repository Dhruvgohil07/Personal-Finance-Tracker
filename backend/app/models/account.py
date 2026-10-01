"""A user's bank accounts and cards (SPEC §5 `accounts`).

Every statement upload (Step 7) belongs to one account, and so does every
transaction. Deleting a user deletes their accounts (ON DELETE CASCADE).

Privacy (SPEC §7.3): only the LAST 4 DIGITS of the account/card number are
ever stored. A CHECK constraint enforces this in the database itself, so
even a bug in our code can't write a full account number here.
"""

import uuid
from enum import StrEnum

from sqlalchemy import CheckConstraint, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class BankCode(StrEnum):
    """Which bank the account is at. Picks the statement parser (Step 6).

    GENERIC = any other bank; its CSVs are read with a column mapping the
    user provides. Adding a bank = add a value here + a new migration
    (VARCHAR + CHECK, see ADR 002).
    """

    HDFC = "HDFC"
    SBI = "SBI"
    ICICI = "ICICI"
    GENERIC = "GENERIC"


class AccountType(StrEnum):
    SAVINGS = "savings"
    CURRENT = "current"
    CREDIT_CARD = "credit_card"


def _enum_column(enum_cls: type[StrEnum], name: str) -> Enum:
    # Same settings as Category.kind: VARCHAR + CHECK, storing the values.
    return Enum(
        enum_cls,
        native_enum=False,
        create_constraint=True,
        name=name,
        length=20,
        values_callable=lambda cls: [member.value for member in cls],
    )


class Account(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "accounts"
    __table_args__ = (
        # One row per real account: the same user can't add "ICICI ...1234"
        # twice. Other users CAN have an ICICI ...1234 too (different people).
        # Like categories, no separate user_id index is needed: this
        # constraint starts with user_id, so "WHERE user_id = ..." uses it.
        UniqueConstraint("user_id", "bank_code", "masked_number"),
        # `~` is Postgres' regular-expression match: exactly 4 digits.
        # Named "masked_number_4_digits" -> ck_accounts_masked_number_4_digits.
        CheckConstraint("masked_number ~ '^[0-9]{4}$'", name="masked_number_4_digits"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    bank_code: Mapped[BankCode] = mapped_column(_enum_column(BankCode, "bank_code"))
    # The user's own label, e.g. "Salary account".
    nickname: Mapped[str] = mapped_column(String(50))
    account_type: Mapped[AccountType] = mapped_column(_enum_column(AccountType, "account_type"))
    # Last 4 digits only, kept as TEXT so leading zeros ("0042") survive.
    masked_number: Mapped[str] = mapped_column(String(4))

    def __repr__(self) -> str:
        # No masked_number or nickname: keep reprs (which can end up in logs)
        # to IDs only.
        return f"Account(id={self.id}, user_id={self.user_id})"
