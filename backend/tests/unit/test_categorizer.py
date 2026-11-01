"""Tests for app.categorizer: every handler, the chain's order, and purity.

All narrations, VPAs and names are invented (CLAUDE.md rule 8). The VPA
SHAPES of the merchant QR codes are copied from the real ICICI statement
(Phase 2 Step 5), nothing else.
"""

import uuid
from decimal import Decimal
from pathlib import Path

import pytest

import app.categorizer
from app.categorizer.base import CategorySource, Decision, TxnFacts, categorize
from app.categorizer.handlers import (
    KEYWORD_CONFIDENCE,
    MERCHANT_CONFIDENCE,
    P2P_CONFIDENCE,
    RULE_CONFIDENCE,
    KeywordHandler,
    MerchantHandler,
    P2PHandler,
    Rule,
    RuleMatchType,
    UserRuleHandler,
    build_chain,
)
from app.parsers.base import Channel, Direction
from tests.unit.test_parsers_base import FORBIDDEN_IMPORTS, _imported_modules

D, C = Direction.DEBIT, Direction.CREDIT

MERCHANTS = {"ZOMATO": "food-dining", "SWIGGY": "food-dining", "NETFLIX": "subscriptions"}


def facts(
    description: str = "",
    *,
    direction: Direction = D,
    channel: Channel = Channel.OTHER,
    merchant_key: str | None = None,
    vpa: str | None = None,
    dictionary_key: str | None = None,
    name: str | None = None,
    source: CategorySource = CategorySource.NONE,
) -> TxnFacts:
    return TxnFacts(
        direction=direction,
        channel=channel,
        normalized_description=description,
        merchant_key=merchant_key,
        counterparty_vpa=vpa,
        dictionary_key=dictionary_key,
        counterparty_name=name,
        category_source=source,
    )


def upi(
    name_key: str,
    vpa: str,
    *,
    direction: Direction = D,
    description: str = "UPI/X",
    dictionary_key: str | None = None,
    name: str | None = None,
) -> TxnFacts:
    return facts(
        description,
        direction=direction,
        channel=Channel.UPI,
        merchant_key=name_key,
        vpa=vpa,
        dictionary_key=dictionary_key,
        name=name,
    )


CHAIN = build_chain(rules=(), merchant_categories=MERCHANTS)


# --- Decision -----------------------------------------------------------------


def test_a_decision_names_its_category_exactly_once() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        Decision(CategorySource.KEYWORD, Decimal("0.9"))
    with pytest.raises(ValueError, match="exactly one"):
        Decision(
            CategorySource.KEYWORD, Decimal("0.9"), category_slug="salary", category_id=uuid.uuid4()
        )


# --- 1. User manual: never overwritten ------------------------------------------


def test_a_manual_category_is_never_overwritten() -> None:
    # Even a row the chain would categorize with certainty.
    manual = facts(
        "UPI/ZOMATO",
        merchant_key="ZOMATO",
        dictionary_key="ZOMATO",
        source=CategorySource.USER_MANUAL,
    )

    assert categorize(manual, CHAIN) is None


# --- 2. User rules ----------------------------------------------------------------

GROCERY = uuid.uuid4()
RENT = uuid.uuid4()


@pytest.mark.parametrize(
    ("rule", "row"),
    [
        (Rule(RuleMatchType.MERCHANT, "MOONCAFE", GROCERY), facts(merchant_key="MOONCAFE")),
        (Rule(RuleMatchType.VPA, "Asha@OKAXIS", GROCERY), facts(vpa="asha@okaxis")),
        (Rule(RuleMatchType.CONTAINS, "moon", GROCERY), facts("UPI/MOON CAFE/NO REMARK")),
    ],
)
def test_each_rule_type_matches(rule: Rule, row: TxnFacts) -> None:
    decision = UserRuleHandler([rule]).decide(row)

    assert decision == Decision(CategorySource.USER_RULE, RULE_CONFIDENCE, category_id=GROCERY)


@pytest.mark.parametrize(
    "rule",
    [
        Rule(RuleMatchType.MERCHANT, "MOON", GROCERY),  # equality, not prefix
        Rule(RuleMatchType.VPA, "other@ybl", GROCERY),
        Rule(RuleMatchType.CONTAINS, "SUN", GROCERY),
    ],
)
def test_a_rule_that_does_not_match_passes_the_row_on(rule: Rule) -> None:
    row = facts("UPI/MOON CAFE", merchant_key="MOONCAFE", vpa="moon@ybl")

    assert UserRuleHandler([rule]).decide(row) is None


def test_the_lowest_priority_number_wins() -> None:
    rules = [
        Rule(RuleMatchType.CONTAINS, "MOON", RENT, priority=2),
        Rule(RuleMatchType.MERCHANT, "MOONCAFE", GROCERY, priority=1),
    ]
    decision = UserRuleHandler(rules).decide(facts("UPI/MOON CAFE", merchant_key="MOONCAFE"))

    assert decision is not None and decision.category_id == GROCERY


def test_a_user_rule_beats_the_merchant_dictionary() -> None:
    chain = build_chain(
        rules=[Rule(RuleMatchType.MERCHANT, "ZOMATO", GROCERY)], merchant_categories=MERCHANTS
    )

    decision = categorize(facts(merchant_key="ZOMATO", dictionary_key="ZOMATO"), chain)

    assert decision is not None and decision.source is CategorySource.USER_RULE


# --- 3. Merchant dictionary -----------------------------------------------------
# The handler reads `dictionary_key`, which the caller looked up once (a key
# ICICI cut short, "SWIGGYLIM" -> SWIGGY, is tested in test_narration.py).


@pytest.mark.parametrize(
    ("dictionary_key", "slug"),
    [("ZOMATO", "food-dining"), ("NETFLIX", "subscriptions"), ("SWIGGY", "food-dining")],
)
def test_a_merchant_debit_gets_the_merchants_category(dictionary_key: str, slug: str) -> None:
    decision = MerchantHandler(MERCHANTS).decide(facts(dictionary_key=dictionary_key))

    assert decision == Decision(CategorySource.MERCHANT, MERCHANT_CONFIDENCE, category_slug=slug)


def test_a_merchant_credit_is_a_refund() -> None:
    decision = MerchantHandler(MERCHANTS).decide(facts(direction=C, dictionary_key="ZOMATO"))

    assert decision is not None and decision.category_slug == "refunds"


@pytest.mark.parametrize(
    "dictionary_key",
    [
        None,  # no merchant
        "MOONCAFE",  # a key the handler's dictionary does not hold
    ],
)
def test_an_unknown_merchant_passes_the_row_on(dictionary_key: str | None) -> None:
    assert MerchantHandler(MERCHANTS).decide(facts(dictionary_key=dictionary_key)) is None


def test_salary_from_a_merchant_employer_is_salary_not_a_refund() -> None:
    # Swiggy is a seeded merchant AND an employer: its salary credit is
    # income. The merchant handler passes it on and the keyword rule decides.
    row = facts(
        "NEFT CR-HDFC -SWIGGY LTD-SALARY SEP",
        direction=C,
        channel=Channel.NEFT,
        dictionary_key="SWIGGY",
    )

    assert MerchantHandler(MERCHANTS).decide(row) is None
    decision = categorize(row, CHAIN)
    assert decision is not None and decision.category_slug == "salary"
    assert decision.source is CategorySource.KEYWORD


def test_a_merchants_upi_credit_with_a_salary_remark_is_still_a_refund() -> None:
    # A remark is the payer's text, so it does not make a refund income.
    row = upi(
        "ZOMATO",
        "zomato@hdfcbank",
        direction=C,
        description="UPI/ZOMATO/SALARY",
        dictionary_key="ZOMATO",
    )

    decision = categorize(row, CHAIN)

    assert decision is not None and decision.category_slug == "refunds"


# --- 4. Keywords ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "slug"),
    [
        # Seen in the real statement
        (facts("NFS/CASH WDL/ / /MUMBAI CEN/", channel=Channel.ATM), "cash-withdrawal"),
        (
            facts(
                "UPI/NPCI BHIM/NPCIBHIMCASH@H/BHIMCASHBA/HDFC BANK/ /X",
                direction=C,
                channel=Channel.UPI,
                merchant_key="NPCIBHIM",
            ),
            "refunds",
        ),
        # SPEC §8's examples, not seen in a real statement yet
        (facts("INT.PD: :01-07-2026 TO 30-09-2026", direction=C), "interest"),
        (facts("SAVINGS INTEREST CREDIT", direction=C), "interest"),
        (
            facts(
                "NEFT CR-HDFC -ACME TECH SOLUTIONS PVT LTD-SALARY SEP",
                direction=C,
                channel=Channel.NEFT,
            ),
            "salary",
        ),
        (facts("NACH/EMI PAYMENT TO/ICICI BANK", channel=Channel.ACH_NACH), "emi-loans"),
        (facts("SMS CHARGES FOR QTR JUN-26 INCL GST"), "fees-charges"),
        (facts("ATM CHRG"), "fees-charges"),
    ],
)
def test_keyword_rules(row: TxnFacts, slug: str) -> None:
    decision = KeywordHandler().decide(row)

    assert decision == Decision(CategorySource.KEYWORD, KEYWORD_CONFIDENCE, category_slug=slug)


@pytest.mark.parametrize(
    "row",
    [
        # Direction matters: the same words the other way round mean nothing
        facts("NFS/CASH WDL", channel=Channel.ATM, direction=C),
        facts("SALARY ADVANCE REPAID"),  # a debit mentioning salary
        facts("INTEREST ON LOAN"),  # interest PAID is not income
        facts("SMS CHARGES REVERSED", direction=C),
        # NACH without EMI, and EMI without NACH
        facts("NACH/MUTUAL FUND SIP", channel=Channel.ACH_NACH),
        facts("UPI/EMI SHOP", channel=Channel.UPI),
        # Whole words only: "SALARYMAN" is not "SALARY"
        facts("NEFT CR-SALARYMAN STORE", direction=C, channel=Channel.NEFT),
        facts("UPI/MOON CAFE"),
        # A remark the PAYER typed in UPI or IMPS is not bank wording (the
        # review reproduced "charges" -> Fees & Charges for a person)
        facts("UPI/RAMESH KUM/ @UPI/CHARGES/YES BANK L/ /X", channel=Channel.UPI),
        facts("UPI/ASHA VERMA/ @UPI/SALARY/YES BANK L/ /X", direction=C, channel=Channel.UPI),
        facts("MMT/IMPS/ /INTEREST/ASHA TRADE/SBI", direction=C, channel=Channel.IMPS),
        # Cashback is BHIM's only when NPCI pays it, not a friend's remark
        facts(
            "UPI/ASHA VERMA/ @UPI/CASHBACK/YES BANK L/ /X",
            direction=C,
            channel=Channel.UPI,
            merchant_key="ASHAVERMA",
        ),
    ],
)
def test_keyword_rules_do_not_fire_on_lookalikes(row: TxnFacts) -> None:
    assert KeywordHandler().decide(row) is None


# --- 5. Person-to-person ----------------------------------------------------------


@pytest.mark.parametrize(
    "vpa",
    [
        "9876543210@ybl",  # a phone number
        "919876543210@paytm",
        "asha.verma@okaxis",  # a personal handle
        "ashaverma2001@",  # cut short by the bank (Step 5)
        "ravi.k@ok",
    ],
)
def test_a_upi_payment_to_a_person(vpa: str) -> None:
    decision = P2PHandler().decide(upi("ASHAVERMA", vpa))

    assert decision == Decision(
        CategorySource.HEURISTIC, P2P_CONFIDENCE, category_slug="transfers-to-people"
    )


def test_money_received_from_a_person_is_other_income() -> None:
    decision = P2PHandler().decide(upi("ASHAVERMA", "9876543210@ybl", direction=C))

    assert decision is not None and decision.category_slug == "other-income"


@pytest.mark.parametrize(
    "vpa",
    [
        # Merchant QR codes: the five shapes seen in the real .xls
        "q123456789@ybl",
        "gpay-123456789",
        "paytmqr1234567",
        "paytm-12345678",
        "vyapar.1234567",
    ],
)
def test_a_merchant_qr_code_is_not_a_person(vpa: str) -> None:
    assert P2PHandler().decide(upi("MOONCAFE", vpa)) is None


@pytest.mark.parametrize(
    "vpa",
    [
        # Google Pay for Business's handle (the real October PDF): whole,
        # and as ICICI prints it when the cut at 14 characters takes more
        # and more of it (review of Step 11)
        "ab@okbizaxis",
        "ab1234@okbizax",
        "shop12345@okbi",
        "shopname12@okb",
    ],
)
def test_a_business_handle_is_not_a_person(vpa: str) -> None:
    assert P2PHandler().decide(upi("MOONCAFE", vpa)) is None


@pytest.mark.parametrize(
    "vpa",
    [
        "ab1234@okaxis",  # Google Pay's personal handle
        "okbiz@ybl",  # "okbiz" before the "@" says nothing
        "okbizaxis@ybl",
        "shopname123@ok",  # cut to "ok": could be okaxis, okicici, ...
        "ab@okb",  # not at the cut width: printed whole, a different handle
        "abcdefghijklmn",  # cut before the "@": no handle at all
    ],
)
def test_a_personal_or_unknowable_handle_is_still_a_person(vpa: str) -> None:
    """Only the HANDLE marks a business, and only when enough of it is
    printed to tell it from Google Pay's personal handles."""
    assert P2PHandler().decide(upi("ASHAVERMA", vpa)) is not None


@pytest.mark.parametrize(
    "description",
    [
        "UPI/SHREE GANESH STORES/X",
        "UPI/MOON CAFE/X",
        "UPI/CITY MEDICAL/X",
        "UPI/ACME PVT/X",
    ],
)
def test_a_business_word_means_it_is_not_a_person(description: str) -> None:
    # No known layout (no counterparty name): the whole narration is read.
    assert P2PHandler().decide(upi("X", "9876543210@ybl", description=description)) is None


def test_a_business_word_in_the_name_field_means_it_is_not_a_person() -> None:
    row = upi("CITYMEDICAL", "9876543210@ybl", name="CITY MEDICAL", description="UPI/CITY MEDICAL")

    assert P2PHandler().decide(row) is None


def test_a_business_word_in_the_remark_does_not_stop_p2p() -> None:
    # Paying a friend back for a hotel stay: the remark says what the money
    # was for, the NAME says who got it (review of Step 6).
    row = upi(
        "ASHAVERMA",
        "9876543210@upi",
        name="ASHA VERMA",
        description="UPI/ASHA VERMA/ @UPI/HOTEL/STATE BANK/ /X",
    )

    decision = P2PHandler().decide(row)

    assert decision is not None and decision.category_slug == "transfers-to-people"


@pytest.mark.parametrize(
    "row",
    [
        facts(channel=Channel.UPI, merchant_key="ASHAVERMA"),  # no VPA at all
        facts(channel=Channel.IMPS, vpa="9876543210@ybl"),  # not UPI
    ],
)
def test_p2p_needs_a_upi_row_with_a_vpa(row: TxnFacts) -> None:
    assert P2PHandler().decide(row) is None


# --- The chain --------------------------------------------------------------------


def test_the_dictionary_beats_the_p2p_guess() -> None:
    # A merchant paid through a personal-looking VPA is still the merchant.
    decision = categorize(upi("ZOMATO", "zomato.payments@okaxis", dictionary_key="ZOMATO"), CHAIN)

    assert decision is not None and decision.source is CategorySource.MERCHANT


def test_a_keyword_beats_the_p2p_guess() -> None:
    # BHIM cashback arrives from a personal-looking VPA.
    row = upi(
        "NPCIBHIM", "npcibhimcash@h", direction=C, description="UPI/NPCI BHIM/BHIMCASHBA/HDFC"
    )

    decision = categorize(row, CHAIN)

    assert decision is not None and decision.category_slug == "refunds"
    assert decision.source is CategorySource.KEYWORD


def test_nothing_matched_leaves_the_row_uncategorized() -> None:
    # A local shop's QR code: not in the dictionary, not a person.
    assert categorize(upi("MOONCAFE", "q123456789@ybl"), CHAIN) is None


def test_the_chain_order() -> None:
    names = [type(handler).__name__ for handler in CHAIN]

    assert names == ["UserRuleHandler", "MerchantHandler", "KeywordHandler", "P2PHandler"]


# --- Purity (Phase 2 plan, hard constraint 4) ----------------------------------------

# The same guard as app/parsers' (tests/unit/test_parsers_base.py), with
# the parsers added to what the categorizer may use.
ALLOWED_APP_MODULES = frozenset({"app.categorizer", "app.parsers", "app.utils"})
CATEGORIZER_FILES = sorted(Path(app.categorizer.__file__).parent.glob("*.py"))


@pytest.mark.parametrize("path", CATEGORIZER_FILES, ids=lambda path: path.name)
def test_categorizer_package_imports_nothing_heavy(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    for module in _imported_modules(source, package="app.categorizer"):
        top_level = module.split(".")[0]
        assert top_level not in FORBIDDEN_IMPORTS, f"{path.name} imports {module}"
        if top_level == "app":
            assert ".".join(module.split(".")[:2]) in ALLOWED_APP_MODULES, (
                f"{path.name} imports {module}"
            )
