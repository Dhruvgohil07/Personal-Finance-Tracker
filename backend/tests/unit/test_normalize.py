"""Tests for app.parsers.normalize: narration -> stable string, and normalize_row.

The narrations here are written by hand (no real statement data, CLAUDE.md
rule 8); they imitate the shapes Indian banks print. The channel, VPA and
merchant-key rules are tested in test_narration.py; the exact output of
`normalize_description` is also pinned by test_golden_fingerprints.py.
"""

from datetime import date

import pytest

from app.parsers.base import Channel, Direction, RawRow
from app.parsers.normalize import normalize_description, normalize_row

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


# --- normalize_row --------------------------------------------------------


def test_normalize_row_fills_every_derived_field() -> None:
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
    assert row.channel is Channel.UPI
    assert row.counterparty_vpa is None  # this narration has no VPA


def test_normalize_row_reads_the_vpa_from_the_raw_text() -> None:
    # Normalization strips the phone number ("9876543210@ybl" -> " @YBL"),
    # so the VPA must come from the RAW narration. This is why
    # `read_narration` is given both forms.
    raw = RawRow(
        row_number=1,
        txn_date=date(2026, 8, 14),
        amount_paise=50000,
        direction=Direction.DEBIT,
        raw_description="UPI/ANIL SHAH/9876543210@ybl/NO REMARK/YES BANK L/512345678901/ICIabc",
    )

    row = normalize_row(raw)

    assert "9876543210" not in row.normalized_description
    assert row.counterparty_vpa == "9876543210@ybl"
    assert row.merchant_key == "ANILSHAH"


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
