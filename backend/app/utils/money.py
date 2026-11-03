"""Money helpers: parse statement amounts into paise, format paise as INR.

Rule: money is never a float. All amounts are stored as integer paise
(Rs 12.50 -> 1250). Floats are binary fractions and can't hold most decimal
values exactly:

    >>> 0.29 * 100
    28.999999999999996        # int() of this gives 28 paise, not 29!

`decimal.Decimal` works in base 10, so Decimal("0.29") * 100 is exactly 29.

Two public functions:

    parse_amount_to_paise("1,250.50 Dr")  -> -125050
    format_inr(12500000)                  -> "₹1,25,000.00"
"""

import re
from decimal import Decimal

# After cleaning, a valid amount is digits with at most 2 decimal places,
# and any commas must group the digits the way an amount is really written:
#
#   1250 / 1250.50    no commas
#   1,25,000.00       Indian: the last 3 digits, then pairs (lakh, crore)
#   125,000.00        international: groups of 3
#
# Anything else is refused rather than guessed. "70,00" is ₹70.00 written
# with a DECIMAL comma (usual in semicolon CSVs); dropping the comma as a
# separator turned it into ₹7,000.00, silently, and a balance column
# scaled the same way hid it from reconciliation (review of PR 34). No
# grouping puts two digits after the last comma, so it can't be read as
# one. We check the shape ourselves because Decimal() also accepts
# strings like "NaN", "Infinity" and "1e5", never valid statement amounts.
_NUMBER = re.compile(
    r"(\d+"  # 1250
    r"|\d{1,2}(,\d{2})*,\d{3}"  # 1,25,000 (also 1,000)
    r"|\d{1,3}(,\d{3})+)"  # 125,000 / 1,000,000
    r"(\.\d{1,2})?"  # .5 / .50
)

# Currency markers that may appear before the number: "₹", "Rs", "Rs.", "INR".
_CURRENCY_PREFIX = re.compile(r"^(₹|rs\.?|inr)\s*", re.IGNORECASE)

# A "Cr"/"Dr" marker at the end: "1,250.00 Cr", "1250.00DR", "500 Dr."
_CR_DR_SUFFIX = re.compile(r"\s*(cr|dr)\.?$", re.IGNORECASE)


class MoneyParseError(ValueError):
    """Raised when a string is not a valid amount.

    The message deliberately does NOT include the input text: it is an
    amount (sensitive), and error messages can end up in logs. The parser
    that calls us adds the row number instead.
    """


def parse_amount_to_paise(text: str | None) -> int | None:
    """Parse an amount string from a bank statement into signed integer paise.

    Returns None for a blank cell (e.g. an empty "Debit" column).
    Negative means debit / money out; positive means credit / money in.

    Handles:
        "1,250.50"      -> 125050      (commas: Indian or international grouping)
        "₹ 1,250"       -> 125000      (currency prefix)
        "1,250.50 Cr"   -> 125050      (Cr = credit, positive)
        "1,250.50 Dr"   -> -125050     (Dr = debit, negative)
        "(1,250.50)"    -> -125050     (accounting-style brackets = negative)
        "-1250.5"       -> -125050     (minus sign; 1 decimal place is fine)
        "" / "   "      -> None        (blank cell)

    Raises MoneyParseError for anything else, e.g. "abc", "12.345" (three
    decimal places would silently lose money if rounded), "70,00" (a
    decimal comma: read as grouping it would be 100 times too much), or
    "(100) Cr" (two signs that contradict each other).
    """
    if text is None or not text.strip():
        return None

    cleaned = text.strip()
    negative = False

    # 1. "Cr"/"Dr" suffix. Checked first because it's at the very end.
    suffix = _CR_DR_SUFFIX.search(cleaned)
    has_suffix = suffix is not None
    if suffix is not None:
        negative = suffix.group(1).lower() == "dr"
        cleaned = cleaned[: suffix.start()].strip()

    # 2. Brackets or a leading sign. Only one way of giving the sign is
    #    allowed, so "(100) Cr" or "-100 Dr" are rejected as ambiguous.
    has_brackets = cleaned.startswith("(") and cleaned.endswith(")")
    has_sign = cleaned.startswith(("-", "+"))
    if sum([has_suffix, has_brackets, has_sign]) > 1:  # True counts as 1
        raise MoneyParseError("amount has more than one sign marker")
    if has_brackets:
        negative = True
        cleaned = cleaned[1:-1].strip()
    elif has_sign:
        negative = cleaned[0] == "-"
        cleaned = cleaned[1:].strip()

    # 3. Currency prefix and inner spaces.
    cleaned = _CURRENCY_PREFIX.sub("", cleaned)
    cleaned = cleaned.replace(" ", "")

    # 4. What's left must be a number, its commas (if any) a real grouping;
    #    only then are they dropped.
    if not _NUMBER.fullmatch(cleaned):
        raise MoneyParseError("not a valid amount")
    cleaned = cleaned.replace(",", "")

    # Exact: at most 2 decimal places, so "x 100" always gives a whole number.
    paise = int(Decimal(cleaned) * 100)
    return -paise if negative else paise


def format_inr(paise: int) -> str:
    """Format integer paise as Indian rupees with lakh/crore grouping.

    Indian grouping puts the first comma after 3 digits, then every 2:
        12500000   -> "₹1,25,000.00"      (1.25 lakh)
        1000000000 -> "₹1,00,00,000.00"   (1 crore)
        -125050    -> "-₹1,250.50"
    """
    # bool is a subclass of int in Python, so True would otherwise format
    # as "₹0.01". Floats are rejected so they can never sneak in.
    if isinstance(paise, bool) or not isinstance(paise, int):
        raise TypeError("format_inr expects integer paise")

    sign = "-" if paise < 0 else ""
    # divmod(125050, 100) -> (1250, 50): whole rupees and leftover paise.
    rupees, remainder = divmod(abs(paise), 100)
    return f"{sign}₹{_group_indian(rupees)}.{remainder:02d}"


def _group_indian(number: int) -> str:
    """Insert commas Indian-style: 12500000 -> "1,25,00,000"."""
    digits = str(number)
    if len(digits) <= 3:
        return digits

    # The last 3 digits form one group; everything before is split into 2s.
    head, last_three = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    groups.insert(0, head)
    return ",".join([*groups, last_three])
