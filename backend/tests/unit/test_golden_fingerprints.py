"""Golden test: today's normalized descriptions and fingerprints, pinned.

Why this file exists (Phase 2 plan, hard constraint 1): every stored
transaction's dedupe fingerprint is a sha256 that includes
`normalize_description(raw_description)` (SPEC §6.3, ADR 006). If Step 5's
narration work changed that string by even one character, every row
imported so far would get a different fingerprint, and re-uploading an
overlapping statement would insert all of its rows a second time - the
exact bug rule 5 forbids.

So before any narration code is written, this test records what the code
produces TODAY for a set of narrations shaped like the ones Indian banks
print. The expected values are literal strings and literal hex digests,
copied from a run of the unchanged code, not recomputed by the test: a test
that called `compute_fingerprint` to build its own expectation would agree
with any change.

Channel, VPA and merchant-key detection must be ADDITIVE (new fields on
`NormalizedRow`, kept out of `compute_fingerprint`). If this test fails,
the change broke deduplication; do not "fix" the test by pasting the new
values in. Changing normalization on purpose needs a migration that
recomputes the stored fingerprints (see progress.md, Open decisions).

`merchant_key` is deliberately NOT pinned here: it is not part of the
fingerprint, and Step 5 is meant to improve it.

All narrations are synthetic (CLAUDE.md rule 8): invented names, invented
reference numbers, invented VPAs.
"""

import uuid
from dataclasses import dataclass
from datetime import date

import pytest

from app.parsers.base import Direction, RawRow
from app.parsers.fingerprint import fingerprints_for
from app.parsers.normalize import normalize_description, normalize_row

# A fixed account id: it is part of every fingerprint.
ACCOUNT_ID = uuid.UUID("0b8f3c1e-5a4d-4e2b-9c7f-1d2e3f405162")


@dataclass(frozen=True)
class GoldenCase:
    """One statement row and the two values pinned for it."""

    raw_description: str
    normalized: str
    txn_date: date
    amount_paise: int
    direction: Direction
    balance_after_paise: int | None
    fingerprint: str


D, C = Direction.DEBIT, Direction.CREDIT

CASES = [
    # UPI to a merchant: reference number stripped, everything else kept.
    GoldenCase(
        "UPI/428913456789/Payment to/ZOMATO ONLINE/HDFC BANK LTD",
        "UPI/ /PAYMENT TO/ZOMATO ONLINE/HDFC BANK LTD",
        date(2026, 9, 1), 45_000, D, 4_955_000,
        "afd37d734b9fc02fbbc79c9baa536653e50b2f019bffd690367d500a6a615310",
    ),
    # UPI to a phone-number VPA: the digits go, "@YBL" stays.
    GoldenCase(
        "UPI/512345678901/9876543210@ybl/Paid via app/YES BANK",
        "UPI/ / @YBL/PAID VIA APP/YES BANK",
        date(2026, 9, 2), 50_000, D, None,
        "e777b55617d656656ff6084206ff72e084e9f3aab378c19f751dbb1d5ac83e72",
    ),
    # UPI to a personal-handle VPA (a dot and lowercase in the handle).
    GoldenCase(
        "UPI/601234567890/ramesh.k@okicici/Rent Sept/ICICI Bank",
        "UPI/ /RAMESH.K@OKICICI/RENT SEPT/ICICI BANK",
        date(2026, 9, 3), 1_500_000, D, 3_455_000,
        "65eb80fa0ccae8ed76b2e53ff149c1420e48cb22710a07bcf353c1c0ce12e2ce",
    ),
    # NEFT salary credit: the IFSC's digit part is stripped, "HDFC" stays.
    GoldenCase(
        "NEFT CR-HDFC0001234-Acme Tech Solutions Pvt Ltd-Salary Sep",
        "NEFT CR-HDFC -ACME TECH SOLUTIONS PVT LTD-SALARY SEP",
        date(2026, 9, 1), 8_500_000, C, 13_455_000,
        "3b367d1e6d08b8db12cb03cf0cf3e4b7ab68b3d46cd2d35e22a1e1f1a1deaef6",
    ),
    GoldenCase(
        "IMPS/P2A/612345678912/Asha Traders/SBIN",
        "IMPS/P2A/ /ASHA TRADERS/SBIN",
        date(2026, 9, 4), 230_000, D, 13_225_000,
        "31af59a4691bd090bda66c77926cde8cfcc046f1676d923a22c002fd82cb0382",
    ),
    # A UTR with letters: only the trailing digit run is stripped.
    GoldenCase(
        "RTGS/UTIBR52026091512345678/Northwind Exports",
        "RTGS/UTIBR /NORTHWIND EXPORTS",
        date(2026, 9, 5), 25_000_000, C, 38_225_000,
        "8943644318aef1c1ce1b458537df38606934ca687d3baca2b15c44abeece46d4",
    ),
    # ATM: the 5-digit terminal id survives, the 7-digit reference goes.
    GoldenCase(
        "ATW-48291 Cash Withdrawal 4829130",
        "ATW-48291 CASH WITHDRAWAL",
        date(2026, 9, 6), 1_000_000, D, 37_225_000,
        "ac30ca3737f85552402f7b99aaaa9b9622464a7d9c4c197bcd738e92ae4da82f",
    ),
    # Digits are stripped even inside a token: "LN482913057" -> "LN".
    GoldenCase(
        "ACH D- Acme Finance Ltd -LN482913057",
        "ACH D- ACME FINANCE LTD -LN",
        date(2026, 9, 7), 1_250_000, D, 35_975_000,
        "9747f9440e2867864061ca13f0350bb47dac91274008ea44fe3aa82fb95aa0ec",
    ),
    GoldenCase(
        "NACH/EMI Payment to/ICICI Bank",
        "NACH/EMI PAYMENT TO/ICICI BANK",
        date(2026, 9, 7), 300_000, D, None,
        "dff558b83817fe6288cf502bf1afa1728343dccec8ddb087b07b0662635ecafe",
    ),
    # Interest credit: the dates (short digit groups) survive.
    GoldenCase(
        "Int.Pd:012345678901:01-07-2026 to 30-09-2026",
        "INT.PD: :01-07-2026 TO 30-09-2026",
        date(2026, 9, 30), 4_321, C, 35_679_321,
        "a45ab6af71a8a9e9452be857f581882f338b7acaf5b2465e4f96d6664a4b5ea0",
    ),
    GoldenCase(
        "SMS Charges for Qtr Jun-26 incl GST",
        "SMS CHARGES FOR QTR JUN-26 INCL GST",
        date(2026, 9, 30), 1_770, D, 35_677_551,
        "d2772196bba231532cfae4af4847b8c607dac01183e0414a8cad88aac492a034",
    ),
    GoldenCase(
        "POS 4312 SWIGGY INSTAMART",
        "POS 4312 SWIGGY INSTAMART",
        date(2026, 9, 8), 64_900, D, 35_612_651,
        "ac5d463e762b473690e0269011d0a5792c99a4b0a014cb105b39b97e1a85cb0e",
    ),
    # Exactly six digits (a cheque number) is already "long" and goes.
    GoldenCase(
        "CHQ DEP 000123 Clearing",
        "CHQ DEP CLEARING",
        date(2026, 9, 9), 2_000_000, C, None,
        "8f3ecc604c5976bfb496fdb42eb953bcce2279d568d7d05db99c64e013bcddd1",
    ),
    # A pipe would blur the fingerprint's field boundaries: it becomes a space.
    GoldenCase(
        "ACME|STORES",
        "ACME STORES",
        date(2026, 9, 10), 12_345, D, 37_600_306,
        "c6392533fb02ddb1b479edd48a2a25c2ce0d341ce2b297b5c68bbfde8e2673ea",
    ),
    GoldenCase(
        "  Mixed   case\twith\nwhitespace ",
        "MIXED CASE WITH WHITESPACE",
        date(2026, 9, 11), 99, D, 37_600_207,
        "3349f17a35df38b3497a6cc47835c12b341e3b18e1dd1f1ce366af3bfb3afc81",
    ),
    # A blank narration normalizes to "" and still fingerprints.
    GoldenCase(
        "",
        "",
        date(2026, 9, 12), 100, C, 37_600_307,
        "0d57cca26f3f22a7ccaf810c3c95d6b4f5c7968092ef500ed6a982471fd7aa0d",
    ),
    # Two identical rows on one day with no balance: occurrence 0 and 1,
    # so they get DIFFERENT fingerprints and both are kept.
    GoldenCase(
        "UPI/700000000001/CHAI POINT/paytm",
        "UPI/ /CHAI POINT/PAYTM",
        date(2026, 9, 20), 2_000, D, None,
        "81295542eaf1a38e10ede1d6f49dd665d3d15e0419ae89030ff07036a42fa964",
    ),
    GoldenCase(
        "UPI/700000000001/CHAI POINT/paytm",
        "UPI/ /CHAI POINT/PAYTM",
        date(2026, 9, 20), 2_000, D, None,
        "772396a15b4e65f3b717a4145a3f5254ca312b88be6434d24e60d8bbb15a4538",
    ),
]  # fmt: skip


def _raw_row(number: int, case: GoldenCase) -> RawRow:
    return RawRow(
        row_number=number,
        txn_date=case.txn_date,
        amount_paise=case.amount_paise,
        direction=case.direction,
        raw_description=case.raw_description,
        balance_after_paise=case.balance_after_paise,
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: repr(case.raw_description)[:40])
def test_normalized_description_is_unchanged(case: GoldenCase) -> None:
    assert normalize_description(case.raw_description) == case.normalized


def test_fingerprints_are_unchanged() -> None:
    """The whole statement fingerprinted at once, as the importer does it.

    Going through `fingerprints_for` (not `compute_fingerprint` per row)
    also pins `assign_occurrence_indexes`: the last two cases are identical
    rows and only differ by their occurrence index.
    """
    rows = [normalize_row(_raw_row(number, case)) for number, case in enumerate(CASES, 1)]

    actual = fingerprints_for(ACCOUNT_ID, rows)

    assert actual == [case.fingerprint for case in CASES]


def test_the_two_identical_rows_really_differ() -> None:
    """Guards the golden data itself: the chai pair must not collapse."""
    assert CASES[-1].fingerprint != CASES[-2].fingerprint
