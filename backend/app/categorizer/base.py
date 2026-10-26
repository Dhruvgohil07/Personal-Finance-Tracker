"""The categorizer's building blocks: facts in, a decision out (SPEC §8).

SPEC §8 describes a **Chain of Responsibility**: a list of handlers, each of
which either decides the category or passes the transaction to the next one.
The first handler that decides wins. That shape makes the ORDER the policy -
"a user's own rule beats the merchant dictionary, which beats a keyword" -
and keeps every handler small enough to test on its own.

    categorize(facts, chain)
        -> UserRuleHandler      decides?  yes -> done
        -> MerchantHandler      decides?  yes -> done
        -> KeywordHandler       ...
        -> P2PHandler           ...
        -> nobody decided       -> None (the row stays "not categorized yet")

Step 1 of SPEC's chain, "never overwrite the user's manual choice", is not a
handler but a guard at the top of `categorize`: it is not a decision about
the category, it is a refusal to make one.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from app.parsers.base import Channel, Direction


class CategorySource(StrEnum):
    """WHO decided a transaction's category (SPEC §5, §8).

    Stored next to `category_id` because the answer changes what we are
    allowed to do with it: a `user_manual` category must never be
    overwritten by the categorizer, and it is also what teaches a
    `category_rules` row (SPEC §8.1). `none` means "not categorized yet".

    It lives here, in the pure layer that decides it, for the same reason
    `Direction` and `Channel` live in `app/parsers/base.py`: the model
    (`app/models/transaction.py`) imports it from here, never the other way
    round. Moved here in Phase 2 Step 6; still importable from `app.models`.
    """

    USER_MANUAL = "user_manual"  # the user chose it by hand
    USER_RULE = "user_rule"  # one of the user's own rules matched
    MERCHANT = "merchant"  # the merchants dictionary matched
    KEYWORD = "keyword"  # a keyword rule matched
    LLM = "llm"  # the LLM suggested it (Phase 4)
    HEURISTIC = "heuristic"  # a fallback guess (e.g. person-to-person)
    NONE = "none"  # not categorized


@dataclass(frozen=True)
class TxnFacts:
    """Everything a handler may look at: what the import derived, nothing more.

    Deliberately no amount, no date and no raw narration. No handler needs
    them, and a categorizer that never sees an amount cannot leak one (the
    same data Phase 4 may send to an LLM must stay this small, CLAUDE.md
    §7.4).
    """

    direction: Direction
    channel: Channel
    normalized_description: str
    merchant_key: str | None
    counterparty_vpa: str | None
    category_source: CategorySource = CategorySource.NONE


@dataclass(frozen=True)
class Decision:
    """A handler's answer: which category, who decided, and how sure.

    The category is named in exactly ONE of two ways:

    - `category_slug` - a SYSTEM category ("food-dining"). The built-in
      handlers only know system categories, and code refers to them by slug
      (app/models/category.py), never by UUID.
    - `category_id` - the category a user's rule points at (Step 7). It may
      be one of the user's own categories, whose slug is only unique per
      user, so the id is the unambiguous reference.

    `confidence` is a Decimal like the column it is stored in
    (`NUMERIC(3, 2)`), so 0.70 stays exactly 0.70.
    """

    source: CategorySource
    confidence: Decimal
    category_slug: str | None = None
    category_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        # Exactly one of the two: `!=` on two booleans is "exclusive or".
        if (self.category_slug is None) == (self.category_id is None):
            raise ValueError("a decision names its category by slug OR by id, exactly one")


class Handler(Protocol):
    """One step of the chain: decide, or return None to pass it on.

    A `Protocol` (like `StatementParser` in app/parsers/base.py) says "any
    object with this method will do" without a common base class.
    """

    def decide(self, facts: TxnFacts) -> Decision | None: ...


def categorize(facts: TxnFacts, chain: Sequence[Handler]) -> Decision | None:
    """Run the chain; the first handler that decides wins.

    Returns None when the user already chose the category by hand (SPEC §8
    step 1: never overwrite it), or when no handler recognised the row - it
    then stays NULL, the "not categorized yet" bucket (Phase 2 plan).
    """
    if facts.category_source is CategorySource.USER_MANUAL:
        return None
    for handler in chain:
        decision = handler.decide(facts)
        if decision is not None:
            return decision
    return None
