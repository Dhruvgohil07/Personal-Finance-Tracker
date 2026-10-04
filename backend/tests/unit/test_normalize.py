"""Tests for app.parsers.normalize: narration -> stable string + merchant key.

The narrations here are written by hand (no real statement data, CLAUDE.md
rule 8); they imitate the shapes Indian banks print. Phase 2 replaces this
first-pass logic with rules derived from real ICICI narrations, and these
tests will be rewritten with it.
"""

from datetime import date

import pytest

from app.parsers.base import Direction, RawRow
from app.parsers.normalize import (
    merchant_key_candidate,
    normalize_description,
    normalize_row,
)

# --- normalize_description ------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Uppercased
        ("Zomato Online", "ZOMATO ONLINE"),
        # Whitespace runs collapse, ends trimmed
        ("  NEFT   CR-ACME   PVT  LTD ", "NEFT CR-ACME PVT LTD"),
        # Tabs and newlines are whitespace too (PDF cells contain them)
        ("POS\t4312\nSWIGGY", "POS 4312 SWIGGY"),
        # Long digit runs (6+) are reference numbers and go away
        ("UPI/428913456789/ZOMATO", "UPI/ /ZOMATO"),
        # Shorter numbers are meaningful and stay
        ("STORE 42 PURCHASE 2026", "STORE 42 PURCHASE 2026"),
        ("POS 4312 SWIGGY", "POS 4312 SWIGGY"),
        # Pipes become spaces, so they can never blur the fingerprint's
        # field boundaries
        ("ACME|STORES", "ACME STORES"),
        # Already clean input is unchanged
        ("ZOMATO", "ZOMATO"),
        # --- Hand-written ATM / NACH / salary shapes (Step 5 TODO) ---
        # ATM: the 5-digit terminal id survives, the 7-digit reference goes
        ("ATW-48291 Cash Withdrawal 4829130", "ATW-48291 CASH WITHDRAWAL"),
        # NACH/EMI debit: nothing long enough to strip, only uppercased
        ("NACH/EMI Payment to/ICICI Bank", "NACH/EMI PAYMENT TO/ICICI BANK"),
        # Digits are stripped even inside a token: "LN482913057" -> "LN"
        ("ACH D- Acme Finance Ltd -LN482913057", "ACH D- ACME FINANCE LTD -LN"),
        # Salary: the digit part of the IFSC code is stripped, "HDFC" stays
        (
            "NEFT CR-HDFC0001234-Acme Tech Solutions Pvt Ltd-Salary Sep",
            "NEFT CR-HDFC -ACME TECH SOLUTIONS PVT LTD-SALARY SEP",
        ),
    ],
)
def test_normalize_description(raw: str, expected: str) -> None:
    assert normalize_description(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n"])
def test_blank_narration_normalizes_to_empty_string(raw: str | None) -> None:
    # An empty description column is odd but not corrupt: the row's date,
    # amount and balance still identify it.
    assert normalize_description(raw) == ""


def test_normalization_is_stable_for_the_same_transaction() -> None:
    # The property deduplication depends on (SPEC §6.3): the same
    # transaction printed with different spacing/case in two overlapping
    # statements must normalize to one string, or it would be imported twice.
    august = "UPI/428913456789/Payment to/Zomato"
    september = "upi/428913456789/payment  to/ZOMATO "

    assert normalize_description(august) == normalize_description(september)


def test_normalization_is_idempotent() -> None:
    # Normalizing an already-normalized string changes nothing. Worth
    # pinning: Step 7 normalizes once, but re-running a repair job over
    # stored descriptions must not drift.
    once = normalize_description("UPI/428913456789/Zomato   Online")

    assert normalize_description(once) == once


# --- merchant_key_candidate ----------------------------------------------


@pytest.mark.parametrize(
    ("narration", "expected"),
    [
        # The channel word is skipped, the counterparty is taken
        ("UPI/ /PAYMENT TO/ZOMATO ONLINE", "ZOMATO"),
        ("POS 4312 SWIGGY INSTAMART", "SWIGGY"),
        ("NEFT CR-ACME PVT LTD", "ACME"),
        # Company filler never wins over the name
        ("AMAZON INDIA PVT LTD", "AMAZON"),
        ("WWW.FLIPKART.COM", "FLIPKART"),
        # Digits-only and 1-2 character tokens are skipped
        ("ATM 42 IN HSR LAYOUT", "HSR"),
        # Already a bare key
        ("NETFLIX", "NETFLIX"),
        # "D" is too short and ACH is noise, so the lender wins
        ("ACH D- ACME FINANCE LTD -LN", "ACME"),
        # --- Wrong on purpose: today's behaviour, decide in Step 6 ---
        # "CASH" is not a noise word, so an ATM withdrawal gets key CASH.
        # Harmless (keyword rules catch ATW for Cash Withdrawal, SPEC §8),
        # but CASH/WITHDRAWAL could join _NOISE_WORDS.
        ("ATW-48291 CASH WITHDRAWAL", "CASH"),
        # "EMI" is a payment type, not the lender; ICICI was intended.
        ("NACH/EMI PAYMENT TO/ICICI BANK", "EMI"),
        # The IFSC bank code comes before the employer, so a salary credit
        # is keyed by the sender's bank. Phase 2 should skip IFSC prefixes.
        ("NEFT CR-HDFC -ACME TECH SOLUTIONS PVT LTD-SALARY SEP", "HDFC"),
    ],
)
def test_merchant_key_candidate(narration: str, expected: str) -> None:
    assert merchant_key_candidate(narration) == expected


@pytest.mark.parametrize(
    "narration",
    [
        "",  # blank narration
        "UPI/ /TO/ ",  # only noise and short tokens
        "ATM 4312",  # a channel word and a number
    ],
)
def test_merchant_key_is_none_when_nothing_looks_like_a_name(narration: str) -> None:
    assert merchant_key_candidate(narration) is None


def test_merchant_keys_match_the_seeded_merchant_dictionary() -> None:
    # The keys produced here are looked up in `merchants.normalized_key`
    # (SPEC §5), which we seeded as uppercase letters/digits with no
    # punctuation. This checks the two formats agree.
    key = merchant_key_candidate(normalize_description("UPI/428913456789/MakeMyTrip India"))

    assert key == "MAKEMYTRIP"
    assert key.isalnum() and key.isupper()


def test_p2p_narration_returns_the_bank_handle_for_now() -> None:
    # Documents a known Phase 1 limitation rather than endorsing it: for a
    # person-to-person UPI transfer the narration is mostly a VPA, and once
    # the phone number is stripped the bank handle is the first real token.
    # Phase 2 adds VPA extraction + P2P detection (SPEC §8.3), which runs
    # before the merchant lookup; this assertion should change then.
    assert merchant_key_candidate(normalize_description("UPI/9876543210@YBL/9876543210")) == "YBL"


# --- normalize_row --------------------------------------------------------


def test_normalize_row_fills_both_derived_fields() -> None:
    raw = RawRow(
        row_number=3,
        txn_date=date(2026, 8, 14),
        amount_paise=48900,
        direction=Direction.DEBIT,
        raw_description="UPI/428913456789/Payment to/Netflix India",
    )

    row = normalize_row(raw)

    assert row.raw is raw  # the original is kept, untouched
    assert row.normalized_description == "UPI/ /PAYMENT TO/NETFLIX INDIA"
    assert row.merchant_key == "NETFLIX"


def test_normalize_row_keeps_the_raw_description_intact() -> None:
    # We always show the user what the bank actually printed.
    raw = RawRow(
        row_number=1,
        txn_date=date(2026, 8, 14),
        amount_paise=2000,
        direction=Direction.DEBIT,
        raw_description="  Chai  Point  ",
    )

    assert normalize_row(raw).raw.raw_description == "  Chai  Point  "


# The "wrong on purpose" merchant keys above (CASH, EMI, HDFC) record known
# gaps in the first-pass rules. Revisit them in Step 6 with real narrations.
