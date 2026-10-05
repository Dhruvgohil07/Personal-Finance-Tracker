"""One line of a bank statement, stored (SPEC §5 `transactions`).

This is the table everything else in the app reads: insights aggregate it,
budgets compare against it, recurring detection looks for patterns in it.
So its shape is worth reading carefully.

Three groups of columns:

- **What the bank said** - `txn_date`, `value_date`, `amount_paise`,
  `direction`, `balance_after_paise`, `raw_description`. Written once at
  import and never changed.
- **What we derived** - `normalized_description`, `channel`,
  `counterparty_vpa`, `merchant_id`, `fingerprint`. Produced by
  `app/parsers/` from the row above.
- **What the user or the categorizer decided** - `category_id`,
  `category_source`, `category_confidence`, `is_self_transfer`, `notes`.
  These change over the life of a row.

Two invariants the DATABASE enforces, not just our code:

1. `CHECK (amount_paise > 0)`. The sign never lives in the amount; the
   `direction` column says which way the money moved (ADR 001). A query can
   therefore never accidentally add a debit as income.
2. `unique(account_id, fingerprint)`. This is dedupe level 2 (SPEC §6.3):
   the import inserts with `ON CONFLICT DO NOTHING`, so a row already
   present is skipped and counted as a duplicate. That is what makes
   overlapping statements merge cleanly and re-imports harmless
   (CLAUDE.md rule 5) - and it stays correct with two imports running at
   once, because the database decides, not a "does this row exist?" query.
"""

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum_column

# `Direction` lives in the pure parser layer because the parsers are what
# decide it; a model may import from there, never the other way round
# (see app/parsers/base.py).
from app.parsers.base import Direction

# sha256 as hex, same length as statement_uploads.file_sha256.
FINGERPRINT_LENGTH = 64


class Channel(StrEnum):
    """How the money moved (SPEC §5, detected in Phase 2).

    Phase 1 stores `other` for every row: channel detection needs real
    narrations to be written against, which is Phase 2 work (SPEC §6.5).
    The column and its values exist now so that no migration is needed then.
    """

    UPI = "upi"
    CARD = "card"
    NEFT = "neft"
    IMPS = "imps"
    RTGS = "rtgs"
    ATM = "atm"
    CHEQUE = "cheque"
    ACH_NACH = "ach_nach"
    INTEREST = "interest"
    CHARGES = "charges"
    OTHER = "other"


class CategorySource(StrEnum):
    """WHO decided this row's category (SPEC §5, §8).

    Stored next to `category_id` because the answer changes what we are
    allowed to do with it: a `user_manual` category must never be
    overwritten by the categorizer, and it is also what teaches a
    `category_rules` row (SPEC §8.1). `none` means "not categorized yet",
    which is every row in Phase 1 until the user edits it (Step 8).
    """

    USER_MANUAL = "user_manual"  # the user chose it by hand
    USER_RULE = "user_rule"  # one of the user's own rules matched
    MERCHANT = "merchant"  # the merchants dictionary matched
    KEYWORD = "keyword"  # a keyword rule matched
    LLM = "llm"  # the LLM suggested it
    HEURISTIC = "heuristic"  # a fallback guess (e.g. a credit = income)
    NONE = "none"  # not categorized


class Transaction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "transactions"
    __table_args__ = (
        # Dedupe level 2 - see the module docstring.
        UniqueConstraint("account_id", "fingerprint"),
        CheckConstraint("amount_paise > 0", name="amount_positive"),
        # --- Indexes (SPEC §5) ------------------------------------------
        # These are named by hand. Our naming convention builds an index
        # name from its FIRST column only (ix_%(column_0_label)s, see
        # app/db/base.py), and all three of these start with user_id, so
        # they would all end up wanting the same name.
        #
        # The keyset-pagination index (Step 8): `GET /transactions` returns
        # newest first and pages with "give me the rows after this
        # (date, id)". With the index in exactly that order Postgres walks
        # it and stops after `limit` rows - no sort step, and no OFFSET that
        # gets slower the deeper you page.
        #
        # `text("txn_date DESC")` is needed because inside this class body
        # the columns are not attributes yet (they are being defined), so
        # `Transaction.txn_date.desc()` cannot be written here.
        Index(
            "ix_transactions_user_txn_date_id",
            "user_id",
            text("txn_date DESC"),
            text("id DESC"),
        ),
        # Spend per category over a period (SPEC §9.1 insights).
        Index("ix_transactions_user_category_date", "user_id", "category_id", "txn_date"),
        # "everything from this merchant" (top merchants, recurring detection).
        Index("ix_transactions_user_merchant", "user_id", "merchant_id"),
        # Trigram GIN index for the `q=` search in Step 8. A normal b-tree
        # index cannot help `ILIKE '%zomato%'` (a leading wildcard), because
        # a b-tree is ordered by the START of the string. pg_trgm indexes
        # every 3-character slice instead, so a match anywhere in the text
        # can still use the index. The extension is enabled in migration 0001.
        Index(
            "ix_transactions_normalized_description_trgm",
            "normalized_description",
            postgresql_using="gin",
            postgresql_ops={"normalized_description": "gin_trgm_ops"},
        ),
    )

    # --- Ownership --------------------------------------------------------
    # user_id is denormalized on purpose: it could be reached through
    # account_id, but EVERY query filters by it (rule 2), and the indexes
    # above all start with it. A join on every read to recover a column we
    # already know would be slower and easier to forget.
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    # Which import created this row. CASCADE, so deleting an upload
    # (Phase 2) removes exactly the transactions it inserted - the undo
    # button for a bad import.
    upload_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("statement_uploads.id", ondelete="CASCADE")
    )

    # --- What the statement said ------------------------------------------
    # DATE, not timestamp: statements give a day, never a time (CLAUDE.md
    # rule 10). Storing a timestamp would invent a time of day and make
    # "transactions in August" depend on the timezone it was read in.
    txn_date: Mapped[date] = mapped_column(Date())
    # The date the bank actually moved the money, when the statement prints
    # it (many do not). Deliberately NOT part of the fingerprint (ADR 006).
    value_date: Mapped[date | None] = mapped_column(Date())
    # Integer paise, always positive (ADR 001). BIGINT because INTEGER would
    # stop at about ₹2.1 crore, and this is the one column where overflowing
    # silently would be unforgivable.
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    direction: Mapped[Direction] = mapped_column(str_enum_column(Direction, "direction"))
    # The running balance after this transaction, when the statement has a
    # balance column. Used by reconciliation (SPEC §6.4, Phase 2).
    balance_after_paise: Mapped[int | None] = mapped_column(BigInteger)
    # The bank's narration, untouched, so the user can always see what the
    # statement actually said (and so a better parser can re-derive the
    # fields below from it later). Never logged (CLAUDE.md rule 3).
    raw_description: Mapped[str] = mapped_column(Text())

    # --- What we derived --------------------------------------------------
    # The narration after `normalize_description` (SPEC §6.5). Stored, not
    # recomputed on read, because it is part of the fingerprint: changing
    # the normalizer later therefore needs a one-off migration that
    # recomputes both columns (noted in docs/progress.md).
    normalized_description: Mapped[str] = mapped_column(Text())
    channel: Mapped[Channel] = mapped_column(
        str_enum_column(Channel, "channel"), default=Channel.OTHER, server_default="other"
    )
    # The other side's UPI id ("someone@okaxis"), extracted in Phase 2.
    # Personal data: it is never sent to the LLM (SPEC §7.4).
    counterparty_vpa: Mapped[str | None] = mapped_column(Text())
    # SET NULL, not CASCADE: if a merchant row is ever deleted from the
    # global dictionary, the transaction must survive - it only loses the link.
    merchant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("merchants.id", ondelete="SET NULL")
    )

    # --- Categorization ---------------------------------------------------
    # Also SET NULL: deleting a category must not delete the user's
    # transactions, it only makes them uncategorized again.
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL")
    )
    category_source: Mapped[CategorySource] = mapped_column(
        str_enum_column(CategorySource, "category_source"),
        default=CategorySource.NONE,
        server_default="none",
    )
    # How sure that source was (0.00-1.00), for the LLM and the heuristics.
    # NUMERIC, not float: this one is not money, but Decimal keeps the value
    # exactly as the source reported it.
    category_confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    # Money moved between the user's own accounts: excluded from spending
    # and income totals (SPEC §8.4, §9.1). Detected in Phase 2.
    is_self_transfer: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    notes: Mapped[str | None] = mapped_column(Text())

    # --- Dedupe -----------------------------------------------------------
    # sha256 hex of the fields that identify this row (ADR 006), unique per
    # account. Computed in Python by app/parsers/fingerprint.py, so it is
    # always exactly 64 characters - hence a bounded VARCHAR rather than TEXT.
    fingerprint: Mapped[str] = mapped_column(String(FINGERPRINT_LENGTH))

    def __repr__(self) -> str:
        # No amount, no narration, no date: only IDs (CLAUDE.md rule 3).
        return f"Transaction(id={self.id}, account_id={self.account_id})"
