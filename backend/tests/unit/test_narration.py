"""Tests for app.parsers.narration: channel, counterparty VPA, merchant key.

One synthetic narration per pattern found in the real ICICI statement (Phase
2 Step 5, ADR 015), plus the generic first-word rules from SPEC §6.5. Every
name, VPA, reference number and transaction id here is invented (CLAUDE.md
rule 8); only the LAYOUT is copied from the real file.
"""

import pytest

from app.parsers.base import Channel
from app.parsers.narration import (
    Narration,
    detect_channel,
    extract_vpa,
    merchant_key_candidate,
    name_key,
    read_narration,
)
from app.parsers.normalize import normalize_description

# ICICI's 35-character UPI transaction id: a 3-letter prefix + 32 characters.
TXN_ID = "ICI4f2a9c1e7b3d5f6a8c0e2b4d6f8a1c3e"
RRN = "512345678901"


def icici_upi(
    name: str,
    vpa: str,
    remark: str = "NO REMARK",
    bank: str = "YES BANK L",
    *,
    trailing_slash: bool = False,
) -> str:
    """`UPI/<name>/<vpa>/<remark>/<payer bank>/<RRN>/<txn id>[/]`, as ICICI prints it."""
    narration = f"UPI/{name}/{vpa}/{remark}/{bank}/{RRN}/{TXN_ID}"
    return narration + "/" if trailing_slash else narration


def read(raw: str) -> Narration:
    """`read_narration` the way `normalize_row` calls it."""
    return read_narration(raw, normalize_description(raw))


# --- ICICI's UPI layout (real statement: 170 of 171 UPI rows) -------------


@pytest.mark.parametrize(
    ("raw", "vpa", "key"),
    [
        # A PhonePe merchant QR ("Q" + 9 digits @ybl): 45 real rows
        (icici_upi("MOON CAFE", "Q123456789@ybl"), "q123456789@ybl", "MOONCAFE"),
        # The same shop printed without the space, and with ICICI's
        # occasional trailing "/": the SAME key (seen in the real file)
        (
            icici_upi("MOONCAFE", "Q123456789@ybl", trailing_slash=True),
            "q123456789@ybl",
            "MOONCAFE",
        ),
        # VPA cut at 14 characters before its "@": stored as printed
        (
            icici_upi("GREEN LEAF", "gpay-123456789", "UPI", "AXIS BANK"),
            "gpay-123456789",
            "GREENLEAF",
        ),
        (icici_upi("TEA STALL", "paytmqr1234567"), "paytmqr1234567", "TEASTALL"),
        # VPA cut right after the "@", or inside the handle
        (icici_upi("ASHA VERMA", "ashaverma2001@"), "ashaverma2001@", "ASHAVERMA"),
        (
            icici_upi("RAVI KUMAR", "ravi.kumar@ok", "UPI", "STATE BANK"),
            "ravi.kumar@ok",
            "RAVIKUMAR",
        ),
        # A phone-number VPA, complete: a person-to-person payment
        (icici_upi("ANIL SHAH", "9876543210@upi", "Sent using"), "9876543210@upi", "ANILSHAH"),
        # A phone number cut short (no "@" left): still stored as printed
        (icici_upi("NEHA PATEL", "91987654321012"), "91987654321012", "NEHAPATEL"),
        # A credit: the name field is the payer. BHIM cashback, real shape
        (
            icici_upi("NPCI BHIM", "npcibhimcash@h", "BHIMCASHBA", "HDFC BANK"),
            "npcibhimcash@h",
            "NPCIBHIM",
        ),
        # Company filler is dropped so the key can match the dictionary
        (icici_upi("ZOMATO LTD", "zomato@hdfcbank"), "zomato@hdfcbank", "ZOMATO"),
        # Upper-case VPA as printed is lowercased (UPI ids are case-insensitive)
        (icici_upi("DESK SHOP", "DESK.SHOP@ICICI"), "desk.shop@icici", "DESKSHOP"),
    ],
)
def test_icici_upi_reads_name_and_vpa_by_position(raw: str, vpa: str, key: str) -> None:
    assert read(raw) == Narration(Channel.UPI, vpa, key)


def test_icici_upi_never_uses_the_bank_handle_as_the_key() -> None:
    # The Phase 1 gap: the first-word guess turned "9876543210@YBL" into
    # the key "YBL". By position, the key comes from the name field.
    narration = read(icici_upi("KIRAN DESAI", "9876543210@ybl"))

    assert narration.merchant_key == "KIRANDESAI"


def test_icici_upi_with_a_phone_number_as_the_name_has_no_key() -> None:
    # A phone number must never become a merchant key.
    narration = read(icici_upi("9876543210", "9876543210@ybl"))

    assert narration.merchant_key is None
    assert narration.counterparty_vpa == "9876543210@ybl"


def test_icici_upi_with_a_blank_vpa_field_has_no_vpa() -> None:
    assert read(icici_upi("MOON CAFE", " ")).counterparty_vpa is None


def test_a_seven_field_upi_narration_without_an_rrn_is_not_read_by_position() -> None:
    # The RRN in field six is what confirms the layout. Without it the
    # generic rules apply: no complete VPA here, so none is invented.
    narration = read("UPI/MOON CAFE/moon@ybl/NO REMARK/YES BANK L/REF/ABC")

    assert narration.channel is Channel.UPI
    assert narration.counterparty_vpa == "moon@ybl"  # SPEC's pattern still finds it
    assert narration.merchant_key == "MOON"  # first word, not the joined name


# --- Other real ICICI shapes ----------------------------------------------


def test_icici_imps_reads_the_name_field() -> None:
    # "MMT/IMPS/<RRN>/<remark>/<name>/<bank>": 2 real rows, both credits
    narration = read("MMT/IMPS/612345678912/Refund of fee/ASHA TRADE/State Bank of I")

    assert narration == Narration(Channel.IMPS, None, "ASHATRADE")


def test_cash_at_another_banks_atm_has_no_vpa_and_no_merchant() -> None:
    # "NFS/CASH WDL/<RRN>/<terminal>/<place>/<date+time>": 1 real row
    narration = read("NFS/CASH WDL/612345678912/12345678/MUMBAI CEN/15-09-261430")

    assert narration == Narration(Channel.ATM, None, None)


def test_upi_lite_top_up_is_upi_with_no_vpa() -> None:
    # "UPL/<RRN>/UPI L.../<numbers>/ICICI/<txn id>": 1 real row. No known
    # layout, so the generic first word is the key: ICICI's own code "UPL",
    # stable enough for a rule to match (Step 7).
    narration = read(f"UPL/612345678912/UPI L/12345678901234567890 123456789/ICICI/{TXN_ID}")

    assert narration == Narration(Channel.UPI, None, "UPL")


def test_upi_block_with_rrn_first_uses_the_generic_key() -> None:
    # "UPI/<RRN>/remarks/<payee>/<bank>": 1 real row (an amount blocked
    # through a stock exchange). "remarks" is a noise word.
    narration = read("UPI/612345678912/remarks/northwind.bse.b/KOTAK MAHINDRA")

    assert narration == Narration(Channel.UPI, None, "NORTHWIND")


# --- Generic first-word channels (SPEC §6.5; not seen in real data yet) ---


@pytest.mark.parametrize(
    ("raw", "channel"),
    [
        ("NEFT CR-HDFC0001234-Acme Tech Solutions Pvt Ltd-Salary Sep", Channel.NEFT),
        ("RTGS/UTIBR52026091512345678/Northwind Exports", Channel.RTGS),
        ("IMPS/P2A/612345678912/Asha Traders/SBIN", Channel.IMPS),
        ("ACH D- Acme Finance Ltd -LN482913057", Channel.ACH_NACH),
        ("NACH/EMI Payment to/ICICI Bank", Channel.ACH_NACH),
        ("ATM WDL 4312 MG ROAD", Channel.ATM),
        ("ATW-48291 Cash Withdrawal 4829130", Channel.ATM),
        ("POS 4312 SWIGGY INSTAMART", Channel.CARD),
        # Case and leading punctuation/space do not matter
        ("  upi/moon cafe", Channel.UPI),
        # Interest and charges: wording differs by bank, no rule until a
        # real statement shows ICICI's (ADR 015)
        ("Int.Pd:012345678901:01-07-2026 to 30-09-2026", Channel.OTHER),
        ("SMS Charges for Qtr Jun-26 incl GST", Channel.OTHER),
        # Only the FIRST word counts
        ("CASH DEP VIA NEFT BRANCH", Channel.OTHER),
        ("", Channel.OTHER),
        ("///", Channel.OTHER),
    ],
)
def test_detect_channel(raw: str, channel: Channel) -> None:
    assert detect_channel(raw) is channel


def test_generic_upi_finds_a_complete_vpa_and_skips_it_for_the_key() -> None:
    narration = read("UPI/512345678901/asha.k@okaxis/Rent/YES BANK")

    assert narration == Narration(Channel.UPI, "asha.k@okaxis", "RENT")


def test_a_vpa_is_only_extracted_from_upi_rows() -> None:
    # An email address in a NEFT remark is not a counterparty VPA.
    narration = read("NEFT CR-ACME PVT LTD-billing@acme")

    assert narration == Narration(Channel.NEFT, None, "ACME")


def test_generic_atm_withdrawal_has_no_merchant() -> None:
    # The Phase 1 key was "CASH" (wrong on purpose, left open for Phase 2).
    assert read("ATW-48291 Cash Withdrawal 4829130").merchant_key is None


def test_unknown_narration_is_other_with_a_first_word_key() -> None:
    assert read("Acme Stores Bill") == Narration(Channel.OTHER, None, "ACME")


# --- extract_vpa ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("UPI/9876543210@YBL/RENT", "9876543210@ybl"),
        ("to asha.k-2@okaxis today", "asha.k-2@okaxis"),
        # The handle must have letters: a cut-off "name@" is not found
        ("UPI/ashaverma2001@/RENT", None),
        ("NEFT CR-ACME", None),
    ],
)
def test_extract_vpa(raw: str, expected: str | None) -> None:
    assert extract_vpa(raw) == expected


# --- name_key -------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("MOON CAFE", "MOONCAFE"),
        ("Moon Cafe", "MOONCAFE"),
        ("MOON-CAFE.", "MOONCAFE"),
        ("Zomato Pvt", "ZOMATO"),
        ("CAFE 42", "CAFE"),  # digit-only words dropped
        ("9876543210", None),
        ("AB", None),  # too short to mean anything
        ("", None),
    ],
)
def test_name_key(name: str, expected: str | None) -> None:
    assert name_key(name) == expected


# --- merchant_key_candidate (the generic fallback) -------------------------


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
        # A VPA chunk is skipped, so the bank handle is never the key
        ("UPI/ / @YBL/RENT", "RENT"),
        ("UPI/ /ASHA.K@OKAXIS/RENT", "RENT"),
        # --- Still open: no real NACH/NEFT sample to derive a rule from ---
        # "CASH" for a generic ATM narration; `read_narration` returns None
        # for every ATM row, so this key is never stored.
        ("ATW-48291 CASH WITHDRAWAL", "CASH"),
        # "EMI" is a payment type, not the lender.
        ("NACH/EMI PAYMENT TO/ICICI BANK", "EMI"),
        # The IFSC bank code comes before the employer in a salary credit.
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
        "UPI/ @YBL/ ",  # only a VPA: the Phase 1 key was "YBL"
    ],
)
def test_merchant_key_is_none_when_nothing_looks_like_a_name(narration: str) -> None:
    assert merchant_key_candidate(narration) is None


def test_merchant_keys_match_the_seeded_merchant_dictionary() -> None:
    # The keys produced here are looked up in `merchants.normalized_key`
    # (SPEC §5), which we seeded as uppercase letters/digits with no
    # punctuation. This checks the formats agree, for both key builders.
    generic = merchant_key_candidate(normalize_description("UPI/428913456789/MakeMyTrip India"))
    by_name = name_key("MakeMyTrip India")

    assert generic == by_name == "MAKEMYTRIP"
    assert generic.isalnum() and generic.isupper()
