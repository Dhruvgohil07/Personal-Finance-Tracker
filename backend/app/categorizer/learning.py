"""Learning a rule from a correction (SPEC §8.1, Phase 2 Step 7, ADR 017).

When the user re-files one transaction with `apply_to_similar=true`, the
app writes a rule that will file every SIMILAR row the same way. This
module answers the one question that needs judgment: what does "similar"
mean for this row?

    a person (P2P shape, no merchant)  -> their VPA           ("9876543210@upi")
    otherwise, a row with a merchant key -> the merchant key  ("MOONCAFE")
    neither                             -> None: nothing reliable to match on

Why the VPA for a person: a person's merchant key is their NAME as the bank
printed it, cut to 10 characters, and two different "RAVI KUMAR"s would
share it. A VPA belongs to one person. For a business it is the other way
round: one shop or brand pays out through many VPAs (a QR code per counter,
a payment gateway per app), and the key is what they share.

A "contains" rule is never learned: which part of a narration identifies a
counterparty is exactly the guess the two options above avoid. The user can
still write one with `POST /rules`.

Pure, like the rest of the package: facts in, a key out.
"""

from app.categorizer.base import TxnFacts
from app.categorizer.handlers import RuleMatchType, looks_like_person


def learned_rule_key(facts: TxnFacts) -> tuple[RuleMatchType, str] | None:
    """The (match type, pattern) of the rule to learn from this row, or None.

    `facts.dictionary_key` must be filled in (the service does it), because
    "no merchant" is part of the person test: Swiggy pays out from an
    ordinary-looking VPA, and must still be matched by its key.
    """
    vpa = facts.counterparty_vpa
    # `vpa and` repeats a check `looks_like_person` makes, so the type
    # checker also knows the pattern is a str and not None.
    if vpa and facts.dictionary_key is None and looks_like_person(facts):
        return RuleMatchType.VPA, vpa
    if facts.merchant_key:
        return RuleMatchType.MERCHANT, facts.merchant_key
    return None
