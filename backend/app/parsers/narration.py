"""Read HOW the money moved and WHO was on the other side (SPEC §6.5).

`normalize_description` (app/parsers/normalize.py) turns a narration into
the stable string that goes into the dedupe fingerprint. This module reads
three more facts out of the same narration:

    channel           UPI, IMPS, ATM, ...            -> transactions.channel
    counterparty_vpa  the other side's UPI id        -> transactions.counterparty_vpa
    merchant_key      "ZOMATO", "MOONCAFE"           -> transactions.merchant_key

None of the three is part of the fingerprint (Phase 2 hard constraint 1), so
these rules can be improved later without re-importing anything or changing
a single stored fingerprint. The golden test
(tests/unit/test_golden_fingerprints.py) guards that boundary.

**Where the rules come from.** SPEC §6.2 says "do not guess bank formats",
so every rule below is marked with its source. Most come from the 175 real
narrations of the ICICI statement studied in Phase 2 Step 5 (ADR 015). A few
first-word channel keywords come from SPEC §6.5's own list and have not been
seen in a real statement yet; they are labelled as such.

**ICICI's UPI narration is fixed-width.** 170 of the 171 UPI rows in the real
statement have exactly this layout:

    UPI/<name>/<vpa>/<remark>/<payer bank>/<12-digit RRN>/<35-char txn id>[/]
         <=10    <=14   <=10      <=10

Each field is CUT to its width, so a name reads "MOON CAFE " -> "MOON CAFE"
and a 15-character VPA loses its "@handle" completely. Because the layout is
fixed, the name and the VPA are read by POSITION, which is far more reliable
than searching the text for something that looks like a VPA. The RRN in the
sixth field is what confirms the layout; a narration that does not fit falls
back to the generic first-word rules.

Everything here is pure string work (no database, no logging), like the
rest of `app/parsers/`.
"""

import re
from dataclasses import dataclass

from app.parsers.base import Channel

# --- Channel: the narration's first word ----------------------------------

# Banks put the channel first ("UPI/...", "NEFT CR-...", "ATW-48291 ..."), so
# the first word decides. Only the FIRST word: "NEFT" in the middle of a
# narration (a remark, a shop called "NEFT TRADERS") says nothing.
_CHANNEL_BY_FIRST_WORD: dict[str, Channel] = {
    # Seen in the real ICICI statement:
    "UPI": Channel.UPI,
    "UPL": Channel.UPI,  # "UPL/<RRN>/UPI Lite/...": a top-up of the UPI Lite wallet
    "MMT": Channel.IMPS,  # ICICI's name for IMPS: "MMT/IMPS/<RRN>/..."
    "NFS": Channel.ATM,  # "NFS/CASH WDL/...": cash from another bank's ATM (NFS network)
    # From SPEC §6.5's list, NOT yet seen in a real statement (ADR 015):
    "IMPS": Channel.IMPS,
    "NEFT": Channel.NEFT,
    "RTGS": Channel.RTGS,
    "ACH": Channel.ACH_NACH,
    "NACH": Channel.ACH_NACH,
    "ATM": Channel.ATM,
    "ATW": Channel.ATM,
    "POS": Channel.CARD,
}
# Interest and charges are in SPEC §6.5's list too, but their wording is not
# a first word and differs by bank ("Int.Pd", "INTEREST CREDIT", "SMS CHRG").
# They stay OTHER until a real statement shows ICICI's wording.

# --- Merchant key ---------------------------------------------------------

# A merchant key must be comparable with the seeded `merchants.normalized_key`
# values ("ZOMATO", "MAKEMYTRIP"): uppercase letters and digits only. This
# splits a string on everything else, so "ZOMATO-ONLINE.COM" becomes
# ["ZOMATO", "ONLINE", "COM"].
_NOT_KEY_CHARS = re.compile(r"[^A-Z0-9]+")

# Below this length a key carries no meaning on its own ("TO", "A", "X").
_MIN_KEY_LENGTH = 3

# Company-name filler: "ZOMATO PVT LTD" and "Zomato" are one merchant.
_COMPANY_FILLER = frozenset(
    {"PVT", "PRIVATE", "LTD", "LIMITED", "LLP", "INC", "CORP", "INDIA", "ONLINE", "WWW", "COM"}
)

# Words that appear in narrations because of HOW the money moved, not WHO
# received it. Used only by the generic first-word fallback below.
_NOISE_WORDS = _COMPANY_FILLER | frozenset(
    {
        # Payment channels and instrument types
        "UPI",
        "MMT",  # ICICI's IMPS prefix (real statement)
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
        "REMARK",  # ICICI: "NO REMARK" / "remarks" (real statement)
        "REMARKS",
        "FROM",
        "WITHDRAWAL",
        "DEPOSIT",
        "BANK",
        "MERCHANT",
    }
)

# --- VPA ------------------------------------------------------------------

# SPEC §6.5's VPA pattern, for narrations that are NOT in ICICI's fixed
# layout. It needs the "@handle", so it cannot find a VPA ICICI has cut
# short - which is why the ICICI layout is read by position instead.
_VPA = re.compile(r"[a-zA-Z0-9.\-_]+@[a-zA-Z]+")

# Anything around an "@" up to the next space or "/": what is left of a VPA
# after normalization ("9876543210@YBL" -> " @YBL"). Removed before picking
# a merchant key, so a person-to-person transfer no longer gets the bank
# handle "YBL" as its key (the known Phase 1 gap).
_VPA_CHUNK = re.compile(r"[^\s/]*@[^\s/]*")

# --- ICICI's fixed layouts (real statement) -------------------------------

# A UPI reference number (RRN) is exactly 12 digits.
_RRN = re.compile(r"\d{12}")


@dataclass(frozen=True)
class Narration:
    """What `read_narration` found. Any field may be "don't know"."""

    channel: Channel
    counterparty_vpa: str | None
    merchant_key: str | None


def read_narration(raw_description: str, normalized_description: str) -> Narration:
    """Read the channel, the counterparty VPA and a merchant key.

    Takes both forms of the narration: the RAW one, because a VPA must be
    read before normalization strips its digits ("9876543210@ybl" would
    become " @YBL"), and the NORMALIZED one for the generic merchant key.
    `normalize_row` passes both, which is what callers should use.

    In this order:
        1. ICICI's UPI layout   -> name and VPA by position
        2. ICICI's IMPS layout  -> name by position
        3. a cash withdrawal    -> no VPA and no merchant (cash has none)
        4. anything else        -> first-word channel, SPEC's VPA pattern
                                   (UPI only) and the first name-like word
    """
    channel = detect_channel(raw_description)
    fields = _fields(raw_description)

    if channel is Channel.UPI and _is_icici_upi(fields):
        return Narration(channel, _printed_vpa(fields[2]), name_key(fields[1]))
    if channel is Channel.IMPS and _is_icici_imps(fields):
        return Narration(channel, None, name_key(fields[4]))
    if channel is Channel.ATM:
        return Narration(channel, None, None)

    vpa = extract_vpa(raw_description) if channel is Channel.UPI else None
    return Narration(channel, vpa, merchant_key_candidate(normalized_description))


def detect_channel(raw_description: str) -> Channel:
    """The channel named by the narration's first word, else OTHER.

    "UPI/MOON CAFE/..."          -> UPI
    "NEFT CR-HDFC0001234-..."    -> NEFT
    "ATW-48291 Cash Withdrawal"  -> ATM
    "Int.Pd:..."                 -> OTHER  (no rule yet, see above)
    """
    for word in _NOT_KEY_CHARS.split(raw_description.upper()):
        if word:
            return _CHANNEL_BY_FIRST_WORD.get(word, Channel.OTHER)
    return Channel.OTHER


def extract_vpa(raw_description: str) -> str | None:
    """The first complete VPA in a narration, lowercased (SPEC §6.5 pattern).

    For narrations outside ICICI's fixed layout. Lowercased because UPI ids
    are case-insensitive, so "Asha@OKAXIS" and "asha@okaxis" are one person.
    """
    match = _VPA.search(raw_description)
    return match.group(0).lower() if match else None


def name_key(name: str) -> str | None:
    """A merchant key from a counterparty NAME field: the whole field, joined.

        "MOON CAFE"   -> "MOONCAFE"
        "MOONCAFE"    -> "MOONCAFE"   (the same shop, printed both ways)
        "Zomato Ltd"  -> "ZOMATO"     (company filler dropped)
        "9876543210"  -> None         (digits only: a phone number, not a name)

    The WHOLE field rather than its first word (decided with Dhruv, ADR 015):
    the real statement prints one shop as both "SHINE STAR" and "SHINESTAR",
    which only joining turns into one key. Digit-only words are dropped so a
    phone number printed as a name never becomes a key.
    """
    words = [
        word
        for word in _NOT_KEY_CHARS.split(name.upper())
        if word and not word.isdigit() and word not in _COMPANY_FILLER
    ]
    key = "".join(words)
    return key if len(key) >= _MIN_KEY_LENGTH else None


def merchant_key_candidate(normalized_description: str) -> str | None:
    """Guess the merchant from a narration with no known layout.

    Takes the first word that looks like a name: not a channel or filler
    word (`_NOISE_WORDS`), not purely digits, at least three characters,
    and not part of a VPA.

        "UPI/ /PAYMENT TO/ZOMATO ONLINE" -> "ZOMATO"
        "POS 4312 SWIGGY INSTAMART"      -> "SWIGGY"
        "UPI/ / @YBL/RENT"               -> "RENT"    (not the handle "YBL")
        "UPI/ /TO/ "                     -> None      (nothing left)

    This is the Phase 1 first pass, kept as the FALLBACK for formats we have
    no real sample of. Indian narrations put the channel first and the
    counterparty right after it, so the first real word is usually the
    merchant. The argument must already have been through
    `normalize_description` (the split assumes uppercase).
    """
    without_vpas = _VPA_CHUNK.sub(" ", normalized_description)
    for word in _NOT_KEY_CHARS.split(without_vpas):
        if len(word) < _MIN_KEY_LENGTH or word.isdigit() or word in _NOISE_WORDS:
            continue
        return word
    return None


# --- Helpers --------------------------------------------------------------


def _fields(raw_description: str) -> list[str]:
    """The narration split on "/", each field trimmed.

    ICICI ends some UPI narrations with a "/" and others without (the real
    statement has both for the same shop), so one trailing empty field is
    dropped and both spellings give the same fields.
    """
    fields = [field.strip() for field in raw_description.split("/")]
    if len(fields) > 1 and fields[-1] == "":
        fields.pop()
    return fields


def _is_icici_upi(fields: list[str]) -> bool:
    """`UPI/<name>/<vpa>/<remark>/<payer bank>/<RRN>/<txn id>`, seven fields."""
    return len(fields) == 7 and fields[0].upper() == "UPI" and bool(_RRN.fullmatch(fields[5]))


def _is_icici_imps(fields: list[str]) -> bool:
    """`MMT/IMPS/<RRN>/<remark>/<name>/<bank>`, six fields."""
    return (
        len(fields) == 6
        and fields[0].upper() == "MMT"
        and fields[1].upper() == "IMPS"
        and bool(_RRN.fullmatch(fields[2]))
    )


def _printed_vpa(field: str) -> str | None:
    """The VPA field exactly as printed, lowercased; None when blank.

    Stored even when ICICI cut it short and the "@handle" is gone
    ("gpay-123456789"): one counterparty is always cut the same way, so the
    value still identifies it for P2P detection and VPA rules (decided with
    Dhruv, ADR 015). It is not guaranteed to be a complete, payable VPA.
    """
    return field.lower() or None
