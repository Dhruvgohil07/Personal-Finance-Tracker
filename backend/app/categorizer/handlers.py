"""The handlers of SPEC §8's chain, and `build_chain` to put them in order.

Each handler is a small class with one method, `decide(facts)`. A class
rather than a plain function because two of them carry data (the user's
rules, the merchant dictionary), given once when the chain is built.

Where each rule comes from is marked, as in app/parsers/narration.py: some
from the real ICICI statement, some from SPEC §8's examples that no real
statement has shown yet (decided with Dhruv, ADR 016).
"""

import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.categorizer.base import CategorySource, Decision, Handler, TxnFacts
from app.parsers.base import Channel, Direction
from app.parsers.narration import key_words

# How sure each kind of handler is (stored in `category_confidence`).
RULE_CONFIDENCE = Decimal("1.00")  # the user said so
MERCHANT_CONFIDENCE = Decimal("0.90")  # a known merchant
KEYWORD_CONFIDENCE = Decimal("0.90")  # an unambiguous word
P2P_CONFIDENCE = Decimal("0.70")  # a guess from the VPA's shape (Phase 2 plan)

# System category slugs the built-in handlers use (app/db/seed_data.py).
CASH_WITHDRAWAL = "cash-withdrawal"
EMI_LOANS = "emi-loans"
FEES_CHARGES = "fees-charges"
INTEREST = "interest"
OTHER_INCOME = "other-income"
REFUNDS = "refunds"
SALARY = "salary"
TRANSFERS_TO_PEOPLE = "transfers-to-people"

# Channels whose narration includes a remark TYPED BY THE PAYER (ICICI's UPI
# and IMPS layouts both have one). A bank-wording keyword - "CHARGES",
# "SALARY", "INTEREST" - in such a narration may be the payer's words, not
# the bank's: paying a plumber with the remark "charges" is not a bank fee.
# So those keyword rules only read narrations the BANK wrote (review of
# Step 6).
_PAYER_REMARK_CHANNELS = frozenset({Channel.UPI, Channel.IMPS})


def _words(text: str) -> set[str]:
    """The words of an uppercase text, split exactly as merchant keys are."""
    return set(key_words(text))


# --- 2. User rules (SPEC §8.1; the table arrives in Step 7) ------------------


class RuleMatchType(StrEnum):
    """What a user rule compares (SPEC §5 `category_rules.match_type`)."""

    MERCHANT = "merchant"  # transactions.merchant_key equals the pattern
    VPA = "vpa"  # transactions.counterparty_vpa equals the pattern
    CONTAINS = "contains"  # the normalized narration contains the pattern


@dataclass(frozen=True)
class Rule:
    """One of the user's rules, as plain data (Step 7 loads them from the DB).

    `priority`: a LOWER number is tried first (1 before 2), so "first" in a
    list the user sees and "first" in the chain mean the same thing.
    """

    match_type: RuleMatchType
    pattern: str
    category_id: uuid.UUID
    priority: int = 0

    def matches(self, facts: TxnFacts) -> bool:
        if self.match_type is RuleMatchType.MERCHANT:
            return facts.merchant_key == self.pattern
        if self.match_type is RuleMatchType.VPA:
            return facts.counterparty_vpa == self.pattern.lower()
        # CONTAINS: the narration was uppercased by normalization, so the
        # pattern is too ("zomato" finds "ZOMATO").
        return self.pattern.upper() in facts.normalized_description


class UserRuleHandler:
    """The user's own rules, best priority first. They beat everything built in."""

    def __init__(self, rules: Iterable[Rule]) -> None:
        # sorted() is stable: two rules with the same priority keep the
        # order they were given in.
        self._rules = sorted(rules, key=lambda rule: rule.priority)

    def decide(self, facts: TxnFacts) -> Decision | None:
        for rule in self._rules:
            if rule.matches(facts):
                return Decision(
                    CategorySource.USER_RULE, RULE_CONFIDENCE, category_id=rule.category_id
                )
        return None


# --- 3. Merchant dictionary ---------------------------------------------------


class MerchantHandler:
    """A merchant key found in the merchant dictionary (SPEC §5 `merchants`).

    Reads `facts.dictionary_key`, which the caller looked up once with
    `dictionary_key` (ADR 015 §5a; it also matches the keys ICICI cut short,
    "SWIGGYLIM" -> SWIGGY).

    A DEBIT gets the merchant's category. A CREDIT from a merchant is money
    coming back, so it is a Refund, not spending there (decided with Dhruv,
    ADR 016) - UNLESS it carries an income keyword. Swiggy, Zomato, Amazon,
    Flipkart and Airtel are large employers as well as seeded merchants, and
    their salary credit is income, not a refund. Such a credit is passed on
    for the keyword handler to decide (review of Step 6).
    """

    def __init__(self, merchant_categories: Mapping[str, str]) -> None:
        # dictionary key ("ZOMATO") -> default category slug ("food-dining")
        self._categories = merchant_categories

    def decide(self, facts: TxnFacts) -> Decision | None:
        key = facts.dictionary_key
        if key is None or key not in self._categories:
            return None
        if facts.direction is Direction.DEBIT:
            slug = self._categories[key]
        elif _income_slug(facts) is not None:
            return None
        else:
            slug = REFUNDS
        return Decision(CategorySource.MERCHANT, MERCHANT_CONFIDENCE, category_slug=slug)


# --- 4. Keyword rules -----------------------------------------------------------


class KeywordHandler:
    """Built-in patterns for rows no merchant explains (SPEC §8 step 4).

    Checked in this order; the first that applies wins.
    """

    def decide(self, facts: TxnFacts) -> Decision | None:
        slug = self._slug(facts)
        if slug is None:
            return None
        return Decision(CategorySource.KEYWORD, KEYWORD_CONFIDENCE, category_slug=slug)

    @staticmethod
    def _slug(facts: TxnFacts) -> str | None:
        words = _words(facts.normalized_description)
        debit = facts.direction is Direction.DEBIT
        bank_written = facts.channel not in _PAYER_REMARK_CHANNELS

        # --- Seen in the real ICICI statement ---
        # Cash from an ATM ("NFS/CASH WDL/...", "ATW-..."): the channel says it.
        if facts.channel is Channel.ATM and debit:
            return CASH_WITHDRAWAL
        # BHIM cashback: paid by NPCI itself ("NPCI BHIM"), with the remark
        # printed cut short ("BHIMCASHBA"). Requiring the payer keeps a
        # friend's "cashback" remark out of it.
        if (
            not debit
            and (facts.merchant_key or "").startswith("NPCI")
            and "CASHBA" in facts.normalized_description
        ):
            return REFUNDS

        # --- SPEC §8's examples, NOT yet seen in a real statement ---
        # All bank wording, so none of them reads a payer's remark.
        income = _income_slug(facts)
        if income is not None:
            return income
        if debit and facts.channel is Channel.ACH_NACH and "EMI" in words:
            return EMI_LOANS
        if debit and bank_written and words & {"CHARGES", "CHARGE", "CHRG", "CHGS"}:
            return FEES_CHARGES
        return None


def _income_slug(facts: TxnFacts) -> str | None:
    """Interest or Salary, for a credit whose narration the BANK wrote.

    Shared by `KeywordHandler` and `MerchantHandler` (a merchant's credit
    with an income keyword is income, not a refund). SPEC §8's examples,
    not yet seen in a real statement.
    """
    if facts.direction is not Direction.CREDIT or facts.channel in _PAYER_REMARK_CHANNELS:
        return None
    words = _words(facts.normalized_description)
    if "INTEREST" in words or "INT.PD" in facts.normalized_description:
        return INTEREST
    if "SALARY" in words:
        return SALARY
    return None


# --- 5. Person-to-person heuristic (SPEC §8.3) ----------------------------------

# Merchant QR codes, recognised by the VPA's SHAPE. All five seen in the real
# statement (75 of its UPI rows): a payment to one of these is a shop, never
# a person, even when the shop is not in the dictionary.
_MERCHANT_VPA = re.compile(
    r"q\d{8,}@"  # PhonePe merchant QR: "Q123456789@ybl"
    r"|gpay-\d"  # Google Pay for Business: "gpay-123456789..."
    r"|paytmqr"  # Paytm QR: "paytmqr1234567..."
    r"|paytm-\d"  # Paytm merchant: "paytm-12345678..."
    r"|vyapar\.\d"  # HDFC Vyapar merchant app: "vyapar.1234567..."
)

# Words that say a counterparty is a business even though its VPA looks
# personal (small shops often take payments on a personal UPI id). SPEC §8.3
# says "no business keywords"; this first list is a guess, matched as whole
# words of the narration, and a correction teaches a rule (Step 7).
_BUSINESS_WORDS = frozenset(
    {
        "STORE",
        "STORES",
        "MART",
        "SUPERMARKET",
        "TRADERS",
        "TRADING",
        "ENTERPRISE",
        "ENTERPRISES",
        "AGENCY",
        "AGENCIES",
        "CAFE",
        "RESTAURANT",
        "HOTEL",
        "BAKERY",
        "FOODS",
        "CANTEEN",
        "MEDICAL",
        "MEDICALS",
        "PHARMACY",
        "CHEMIST",
        "HOSPITAL",
        "CLINIC",
        "SERVICES",
        "SOLUTIONS",
        "COLLEGE",
        "SCHOOL",
        "INSTITUTE",
        "UNIVERSITY",
        "PETROL",
        "PVT",
        "LTD",
    }
)


class P2PHandler:
    """A UPI transfer to or from a PERSON (SPEC §8.3, Phase 2 plan).

    All of these must hold:

    - the channel is UPI and the row has a counterparty VPA
    - the VPA is not a merchant QR code (`_MERCHANT_VPA`)
    - no business word in the counterparty's NAME (`_BUSINESS_WORDS`). When
      the layout is known, only the name field is read: a remark such as
      "hotel" says what the money was for, not who received it. Otherwise
      the whole narration is read, as nothing tells the name apart.
    - no merchant matched - guaranteed by the chain's order, since the
      merchant handler runs first

    A debit is "Transfers to People"; a credit is money a person sent you,
    "Other Income" (decided with Dhruv, ADR 016: the summary already counts
    it as income, so the label agrees with the totals). A guess, hence the
    lower confidence and the `heuristic` source.

    These rows contain real people's names, which is why Phase 4 must never
    send a `heuristic` P2P row to the LLM or put its key in the global cache.
    """

    def decide(self, facts: TxnFacts) -> Decision | None:
        vpa = facts.counterparty_vpa
        if facts.channel is not Channel.UPI or not vpa:
            return None
        if _MERCHANT_VPA.match(vpa):
            return None
        who = facts.counterparty_name or facts.normalized_description
        if _words(who) & _BUSINESS_WORDS:
            return None
        slug = TRANSFERS_TO_PEOPLE if facts.direction is Direction.DEBIT else OTHER_INCOME
        return Decision(CategorySource.HEURISTIC, P2P_CONFIDENCE, category_slug=slug)


# --- The chain ------------------------------------------------------------------


def build_chain(*, rules: Iterable[Rule], merchant_categories: Mapping[str, str]) -> list[Handler]:
    """SPEC §8's chain, in its order (step 1, user manual, is `categorize`'s guard).

    Step 6 (the LLM) arrives in Phase 4 between P2P and "nothing matched";
    step 7 (the default) is `categorize` returning None.
    """
    return [
        UserRuleHandler(rules),
        MerchantHandler(merchant_categories),
        KeywordHandler(),
        P2PHandler(),
    ]
