"""A user's own categorization rule (SPEC §5 `category_rules`, §8.1).

"Every transaction whose merchant key is MOONCAFE is Food & Dining." A rule
is written by the user (`POST /rules`) or learned from a correction
(`PATCH /transactions/{id}` with `apply_to_similar=true`), and the
categorizer's user-rule handler tries the rules before anything built in
(ADR 016, ADR 017).

What a rule compares is its `match_type`:

- `merchant` - the row's merchant key equals the pattern ("MOONCAFE")
- `vpa`      - the row's counterparty UPI id equals the pattern
- `contains` - the row's normalized narration contains the pattern

The pattern is stored already normalized for its match type (uppercase key,
lowercase VPA, normalized narration text; see app/schemas/rule.py), so
matching is a plain comparison and the unique constraint below catches
"Moon Cafe" and "MOONCAFE" as the same rule.
"""

import uuid

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

# `RuleMatchType` lives in the pure categorizer, which decides what a match
# means; the model imports it from there, never the other way round (the
# same rule as `CategorySource`, see app/models/transaction.py).
from app.categorizer.handlers import RuleMatchType
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum_column

# Rules without a priority of their own get this one. Lower is tried first,
# so a user who wants one rule to beat the rest gives it a smaller number.
DEFAULT_RULE_PRIORITY = 100


class CategoryRule(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "category_rules"
    __table_args__ = (
        # One rule per thing matched: a second "MOONCAFE" merchant rule
        # would only fight the first. Starts with user_id, so it is also
        # the index for "this user's rules" (no separate one needed).
        UniqueConstraint("user_id", "match_type", "pattern"),
        # The two foreign keys below have ON DELETE actions. When a category
        # or a transaction is deleted, Postgres must find the rules pointing
        # at it, and without an index that is a full scan of this table for
        # EVERY deleted row - deleting an upload deletes hundreds of
        # transactions at once (the lesson of Phase 2 Step 1's review).
        Index("ix_category_rules_category_id", "category_id"),
        Index("ix_category_rules_created_from_transaction_id", "created_from_transaction_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    match_type: Mapped[RuleMatchType] = mapped_column(str_enum_column(RuleMatchType, "match_type"))
    pattern: Mapped[str] = mapped_column(Text())
    # CASCADE: a rule that files rows under a deleted category has nothing
    # left to do, so it goes with the category (Phase 2 plan).
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    priority: Mapped[int] = mapped_column(
        Integer(), default=DEFAULT_RULE_PRIORITY, server_default=str(DEFAULT_RULE_PRIORITY)
    )
    # The row the rule was learned from, if any. SET NULL: deleting that
    # upload must not delete the rule the user taught, only the link.
    created_from_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL")
    )

    def __repr__(self) -> str:
        # No pattern: for a `vpa` rule it is a person's UPI id (rule 3).
        return f"CategoryRule(id={self.id}, match_type={self.match_type.value!r})"
