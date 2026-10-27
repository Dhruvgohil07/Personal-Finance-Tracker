"""Turn a bank narration into a stable, comparable string (SPEC §6.5).

A narration is whatever the bank printed in the description column:

    UPI/428913456789/Payment to/ZOMATO ONLINE/HDFC BANK LTD

Two different jobs need something tidier than that:

1. **Deduplication** (SPEC §6.3). The fingerprint of a row includes its
   normalized description, so normalization must be *stable*: the same
   transaction appearing in two overlapping statements has to produce the
   same string both times, or re-importing would create duplicates.
2. **Categorization and recurring detection** (SPEC §8, §9.2). These want
   the channel, the counterparty's VPA and a short merchant key such as
   `ZOMATO`. Those are read by `app/parsers/narration.py` (Phase 2 Step 5),
   and `normalize_row` below puts both jobs together.

Both are pure string work, so everything here is a plain function with no
state - easy to test, and easy to reason about when a narration surprises
us later.

`normalize_description` is LOAD-BEARING: it is part of every stored
fingerprint, so it must stay byte-for-byte as it is (Phase 2 hard
constraint 1). tests/unit/test_golden_fingerprints.py pins its output.
Better narration reading belongs in `narration.py`, which the fingerprint
never sees.
"""

import re

from app.parsers.base import NormalizedRow, RawRow
from app.parsers.narration import read_narration

# Any run of whitespace (spaces, tabs, newlines inside a PDF cell) becomes
# one single space, so "ZOMATO   ONLINE" and "ZOMATO ONLINE" are equal.
_WHITESPACE = re.compile(r"\s+")

# Six or more digits in a row: a UPI reference (12 digits), a cheque number,
# a phone number, an account number. These change on every transaction, so
# keeping them would make each row look unique and defeat the merchant key.
# The limit is six so that shorter, meaningful numbers survive: a year
# ("2026"), a store number ("STORE 42"), or an amount printed inside the
# narration ("12500").
# Public because a `contains` rule refuses a pattern holding one: it could
# never be found in a stored narration (app/categorizer/handlers.py).
LONG_DIGITS = re.compile(r"\d{6,}")


def normalize_description(raw_description: str | None) -> str:
    """Normalize a narration into the string used for dedupe and matching.

    Steps, in this order:
        1. pipes become spaces  (see the note below)
        2. uppercase            ("Zomato" and "ZOMATO" are one merchant)
        3. long digit runs out  (reference numbers change every time)
        4. whitespace collapsed and trimmed

    Examples:
        "UPI/428913456789/Payment to/Zomato"  -> "UPI/ /PAYMENT TO/ZOMATO"
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
    text = LONG_DIGITS.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def normalize_row(row: RawRow) -> NormalizedRow:
    """Wrap a parsed row with every field derived from its narration.

    This is the single entry point the import service calls, so the
    narration is always read from both of its forms: the raw text (a VPA
    must be read before normalization strips its digits) and the normalized
    one (for the generic merchant key).
    """
    normalized_description = normalize_description(row.raw_description)
    narration = read_narration(row.raw_description, normalized_description)
    return NormalizedRow(
        raw=row,
        normalized_description=normalized_description,
        merchant_key=narration.merchant_key,
        channel=narration.channel,
        counterparty_vpa=narration.counterparty_vpa,
    )
