"""Turn a bank narration into a stable, comparable string (SPEC §6.5).

A narration is whatever the bank printed in the description column:

    UPI/428913456789/Payment to/ZOMATO ONLINE/HDFC BANK LTD

Two different jobs need something tidier than that:

1. **Deduplication** (SPEC §6.3). The fingerprint of a row includes its
   normalized description, so normalization must be *stable*: the same
   transaction appearing in two overlapping statements has to produce the
   same string both times, or re-importing would create duplicates.
2. **Categorization and recurring detection** (SPEC §8, §9.2). These want
   a short merchant key such as `ZOMATO` that can be looked up in the
   `merchants` table and compared month to month.

Both are pure string work, so everything here is a plain function with no
state - easy to test, and easy to reason about when a narration surprises
us later.

Scope, deliberately: this is the *first pass*, written before any real
narration has been studied. Channel detection (UPI / NEFT / ATM / ...) and
UPI VPA extraction are Phase 2, once real ICICI narrations are in front of
us (see the Phase 1 plan). What is here is only what deduplication needs
plus a usable `merchant_key` candidate.
"""

import re

from app.parsers.base import NormalizedRow, RawRow

# Any run of whitespace (spaces, tabs, newlines inside a PDF cell) becomes
# one single space, so "ZOMATO   ONLINE" and "ZOMATO ONLINE" are equal.
_WHITESPACE = re.compile(r"\s+")

# Six or more digits in a row: a UPI reference (12 digits), a cheque number,
# a phone number, an account number. These change on every transaction, so
# keeping them would make each row look unique and defeat the merchant key.
# The limit is six so that shorter, meaningful numbers survive: a year
# ("2026"), a store number ("STORE 42"), or an amount printed inside the
# narration ("12500").
_LONG_DIGITS = re.compile(r"\d{6,}")

# Words that appear in narrations because of *how* the money moved, not
# *who* received it. Dropping them is what turns "UPI/Payment to/ZOMATO"
# into "ZOMATO".
#
# Kept deliberately small and boring: every word here is a word we are sure
# is never a merchant name on its own. Phase 2 will extend it from real
# narrations, and the channel words will move into proper channel detection.
_NOISE_WORDS = frozenset(
    {
        # Payment channels and instrument types
        "UPI",
        "NEFT",
        "IMPS",
        "RTGS",
        "ATM",
        "ATW",
        "ACH",
        "NACH",
        "ECS",
        "POS",
        "CHQ",
        "CHEQUE",
        "CARD",
        "DEBIT",
        "CREDIT",
        # Bookkeeping words around the actual counterparty
        "TXN",
        "TRF",
        "TRANSFER",
        "PAYMENT",
        "PAYMENTS",
        "PMT",
        "PAID",
        "PURCHASE",
        "REF",
        "REFNO",
        "REFERENCE",
        "FROM",
        "WITHDRAWAL",
        "DEPOSIT",
        "BANK",
        "MERCHANT",
        # Company-name filler: "ZOMATO INDIA PVT LTD" -> "ZOMATO"
        "PVT",
        "PRIVATE",
        "LTD",
        "LIMITED",
        "LLP",
        "INC",
        "CORP",
        "INDIA",
        "ONLINE",
        "WWW",
        "COM",
    }
)

# A merchant key must be comparable with the `merchants.normalized_key`
# values we seeded ("ZOMATO", "MAKEMYTRIP", "JIOHOTSTAR"): uppercase
# letters and digits only. This splits a string on everything else, so
# "UPI/ZOMATO-ONLINE.COM" becomes ["UPI", "ZOMATO", "ONLINE", "COM"].
_NOT_KEY_CHARS = re.compile(r"[^A-Z0-9]+")

# Below this length a token carries no meaning on its own ("TO", "A", "X").
_MIN_KEY_LENGTH = 3


def normalize_description(raw_description: str | None) -> str:
    """Normalize a narration into the string used for dedupe and matching.

    Steps, in this order:
        1. pipes become spaces  (see the note below)
        2. uppercase            ("Zomato" and "ZOMATO" are one merchant)
        3. long digit runs out  (reference numbers change every time)
        4. whitespace collapsed and trimmed

    Examples:
        "UPI/428913456789/Payment to/Zomato"  -> "UPI//PAYMENT TO/ZOMATO"
        "  NEFT   Cr-Acme   Pvt  Ltd "        -> "NEFT CR-ACME PVT LTD"
        None / ""                             -> ""

    A blank narration gives "" rather than an error: a statement row with an
    empty description is odd but not corrupt, and the row's date, amount and
    balance still identify it.

    Why replace "|"? The fingerprint joins its fields with "|" (SPEC §6.3).
    If a narration contained a pipe, the boundary between the description
    field and the next field would become ambiguous, and two different rows
    could in principle hash to the same value. Pipes carry no meaning in a
    narration, so turning them into spaces removes the problem at the
    source instead of making the fingerprint logic clever.
    """
    if not raw_description:
        return ""

    text = raw_description.replace("|", " ")
    text = text.upper()
    text = _LONG_DIGITS.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def merchant_key_candidate(normalized_description: str) -> str | None:
    """Guess the merchant from an already-normalized description.

    Takes the first token that looks like a name: not a channel or filler
    word (`_NOISE_WORDS`), not purely digits, at least three characters.

        "UPI//PAYMENT TO/ZOMATO ONLINE"  -> "ZOMATO"
        "POS 4312 SWIGGY INSTAMART"      -> "SWIGGY"
        "NEFT CR-ACME PVT LTD"           -> "ACME"      ("CR" is too short)
        "UPI/ /TO/ "                     -> None        (nothing left)

    "First token" is a guess, and it is the right kind of guess for now:
    Indian narrations put the channel first and the counterparty right
    after it, so the first real word is usually the merchant. It is also
    easy to explain and easy to correct - Phase 2 replaces it with rules
    derived from real narrations, and the user can always fix a category by
    hand, which teaches a `category_rules` row (SPEC §8.1).

    Known limitation, on purpose: for a person-to-person UPI transfer the
    narration is mostly a VPA ("9876543210@YBL"), and after the digits are
    stripped this returns the bank handle ("YBL"). Phase 2 adds VPA
    extraction and P2P detection (SPEC §8.3), which runs *before* the
    merchant lookup and stops such keys being used or sent to an LLM.

    The argument must already have been through `normalize_description`
    (the token split assumes uppercase). `normalize_row` below does both in
    the right order, which is what callers should use.
    """
    for token in _NOT_KEY_CHARS.split(normalized_description):
        if len(token) < _MIN_KEY_LENGTH:
            continue
        if token.isdigit():
            continue
        if token in _NOISE_WORDS:
            continue
        return token
    return None


def normalize_row(row: RawRow) -> NormalizedRow:
    """Wrap a parsed row with its normalized description and merchant key.

    This is the single entry point the import service (Step 7) calls, so
    the two functions above are always applied in the correct order.
    """
    normalized_description = normalize_description(row.raw_description)
    return NormalizedRow(
        raw=row,
        normalized_description=normalized_description,
        merchant_key=merchant_key_candidate(normalized_description),
    )
