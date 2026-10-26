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
than searching the text for something that looks like a VPA. The RRN, second
from the end, is what confirms the layout (so a "/" inside the free-text
remark does not break it); a narration that does not fit falls back to the
generic first-word rules.

Everything here is pure string work (no database, no logging), like the
rest of `app/parsers/`.
"""

import re
from collections.abc import Collection
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

# Channel words that SHOULD still become a merchant key in the generic
# fallback. "UPL/<RRN>/UPI Lite/.../ICICI/...": the only name-like word left
# after the channel words is the bank, so ICICI's own code "UPL" is the most
# useful key for a UPI Lite top-up (a rule can match it).
_KEY_WORTHY_CHANNEL_WORDS = frozenset({"UPL"})

# Words that appear in narrations because of HOW the money moved, not WHO
# received it. Used only by the generic first-word fallback below.
#
# Every channel word of `_CHANNEL_BY_FIRST_WORD` is DERIVED into this set, so
# a channel added there can never become a merchant key by accident (that
# happened once: "MMT" was added as a channel and briefly keyed IMPS rows).
_NOISE_WORDS = (
    (frozenset(_CHANNEL_BY_FIRST_WORD) - _KEY_WORTHY_CHANNEL_WORDS)
    | _COMPANY_FILLER
    | frozenset(
        {
            # Payment instruments that are not a first-word channel rule
            "ECS",
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
)

# A seeded merchant key may match a LONGER merchant key as a prefix only when
# it is at least this long (`dictionary_key`). Shorter seeded keys are common
# starts of Indian names and words: AMUL/"Amulya", UBER/"Uberoi", JIO/"Jiothsna".
_MIN_PREFIX_MATCH = 5

# --- VPA ------------------------------------------------------------------

# SPEC §6.5's VPA pattern, for narrations that are NOT in ICICI's fixed
# layout. It needs the "@handle", so it cannot find a VPA ICICI has cut
# short - which is why the ICICI layout is read by position instead.
#
# One addition to SPEC's pattern: the handle must END there (`(?![\w.])`).
# A UPI handle is one word ("ybl", "okaxis"), so "asha@gmail.com" in a remark
# is an email address, not a VPA, and must not be stored as one.
_VPA = re.compile(r"[a-zA-Z0-9.\-_]+@[a-zA-Z]+(?![\w.])")

# A VPA whose name part is only digits is a phone number (10 digits, or 12
# with the country code 91) or a bank ACCOUNT number ("<account>@<ifsc>",
# 9-18 digits). Account numbers are never stored in full (CLAUDE.md §7.3), so
# only the two phone shapes are kept.
_PHONE = re.compile(r"(?:91)?\d{10}")

# Anything around an "@" up to the next space or "/": what is left of a VPA
# after normalization ("9876543210@YBL" -> " @YBL"). Removed before picking
# a merchant key, so a person-to-person transfer no longer gets the bank
# handle "YBL" as its key (the known Phase 1 gap).
_VPA_CHUNK = re.compile(r"[^\s/]*@[^\s/]*")

# --- ICICI's fixed layouts (real statement) -------------------------------

# A UPI reference number (RRN) is exactly 12 digits.
_RRN = re.compile(r"\d{12}")

# The first word of a narration, for `detect_channel`.
_FIRST_WORD = re.compile(r"[^A-Za-z0-9]*([A-Za-z0-9]+)")


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

    # ICICI's layouts are anchored at BOTH ends: the name and VPA at the
    # start, the RRN near the end. The free-text remark in between may
    # itself contain a "/", which must not shift the fields being read.
    if channel is Channel.UPI and _is_icici_upi(fields):
        return Narration(channel, _safe_vpa(fields[2]), name_key(fields[1]))
    if channel is Channel.IMPS and _is_icici_imps(fields):
        return Narration(channel, None, name_key(fields[-2]))
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
    match = _FIRST_WORD.match(raw_description)
    if match is None:
        return Channel.OTHER
    return _CHANNEL_BY_FIRST_WORD.get(match.group(1).upper(), Channel.OTHER)


def extract_vpa(raw_description: str) -> str | None:
    """The first complete VPA in a narration, lowercased (SPEC §6.5 pattern).

    For narrations outside ICICI's fixed layout. Lowercased because UPI ids
    are case-insensitive, so "Asha@OKAXIS" and "asha@okaxis" are one person.
    An email address is not a VPA, and an account-number VPA is not kept
    (see `_PHONE`).
    """
    match = _VPA.search(raw_description)
    return _safe_vpa(match.group(0)) if match else None


def name_key(name: str) -> str | None:
    """A merchant key from a counterparty NAME field: the whole field, joined.

        "MOON CAFE"   -> "MOONCAFE"
        "MOONCAFE"    -> "MOONCAFE"   (the same shop, printed both ways)
        "Zomato Ltd"  -> "ZOMATO"     (trailing company filler dropped)
        "INDIA GATE"  -> "INDIAGATE"  (filler only counts at the END)
        "9876543210"  -> None         (digits only: a phone number, not a name)

    The WHOLE field rather than its first word (decided with Dhruv, ADR 015):
    the real statement prints one shop both with and without the space
    between its two words, which only joining turns into one key. Digit-only
    words are dropped so a phone number printed as a name never becomes a key.

    ICICI cuts the field at 10 characters, so a key can end in part of a word
    ("Swiggy Lim" -> "SWIGGYLIM"). That is kept: dropping a possibly-cut
    last word would split the shop above again. `dictionary_key` matches
    such keys against the merchant dictionary instead.
    """
    words = [word for word in _NOT_KEY_CHARS.split(name.upper()) if word and not word.isdigit()]
    # Company filler is a SUFFIX ("ZOMATO PVT LTD"). Removed only from the
    # end, and never the last remaining word, so "INDIA GATE" and a shop
    # called "ONLINE" keep their names.
    while len(words) > 1 and words[-1] in _COMPANY_FILLER:
        words.pop()
    key = "".join(words)
    return key if len(key) >= _MIN_KEY_LENGTH else None


def dictionary_key(merchant_key: str | None, known_keys: Collection[str]) -> str | None:
    """The merchant-dictionary key a merchant key belongs to, if any.

    For the categorizer's dictionary step (Step 6), which passes in the
    seeded `merchants.normalized_key` values:

        "ZOMATO"     -> "ZOMATO"   exact match
        "SWIGGYLIM"  -> "SWIGGY"   ICICI cut "Swiggy Limited" at 10 characters
        "NESCAFEDA"  -> "NESCAFE"  a branch name after the brand
        "AMULYAK"    -> None       AMUL is too short to match as a prefix

    An exact match always wins. Otherwise the LONGEST known key the merchant
    key starts with, if that known key has at least `_MIN_PREFIX_MATCH`
    characters (short keys start too many names). Measured on the real
    statement: 16 rows match the dictionary this way, as many as Phase 1's
    first-word keys did, while the stored key keeps the whole name.
    """
    if not merchant_key:
        return None
    if merchant_key in known_keys:
        return merchant_key
    prefixes = [
        known
        for known in known_keys
        if len(known) >= _MIN_PREFIX_MATCH and merchant_key.startswith(known)
    ]
    return max(prefixes, key=len, default=None)


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
    """`UPI/<name>/<vpa>/<remark>/<payer bank>/<RRN>/<txn id>`.

    Seven fields, or more when the remark contains a "/": the RRN is found
    second from the END, so extra fields in the middle are tolerated.
    """
    return len(fields) >= 7 and fields[0].upper() == "UPI" and bool(_RRN.fullmatch(fields[-2]))


def _is_icici_imps(fields: list[str]) -> bool:
    """`MMT/IMPS/<RRN>/<remark>/<name>/<bank>`.

    Six fields, or more when the remark contains a "/": the name is read
    second from the END.
    """
    return (
        len(fields) >= 6
        and fields[0].upper() == "MMT"
        and fields[1].upper() == "IMPS"
        and bool(_RRN.fullmatch(fields[2]))
    )


def _safe_vpa(vpa: str) -> str | None:
    """A VPA as printed, lowercased; None when blank or an account number.

    Kept even when ICICI cut it short and the "@handle" is gone
    ("gpay-123456789"): one counterparty is always cut the same way, so the
    value still identifies it for P2P detection and VPA rules (decided with
    Dhruv, ADR 015). It is not guaranteed to be a complete, payable VPA.

    A name part made only of digits is kept only when it is a phone number
    (`_PHONE`). Anything else all-digit is a bank account number - the real
    statement has 14-digit ones, cut before their "@ifsc" - and only the last
    4 digits of an account number may be stored (CLAUDE.md §7.3). The full
    number still sits in `raw_description`, which is shown, never matched.
    """
    vpa = vpa.strip().lower()
    local_part = vpa.partition("@")[0]
    if local_part.isdigit() and not _PHONE.fullmatch(local_part):
        return None
    return vpa or None
