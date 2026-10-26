"""Opaque cursors for keyset pagination (SPEC §10).

Why keyset pagination and not `OFFSET`:

    OFFSET 10000 LIMIT 50   the database must walk and throw away 10000
                            rows before it can answer, so page 200 is much
                            slower than page 1
    WHERE (txn_date, id) < (cursor)   the database jumps straight into the
                            index at that position, so every page costs
                            the same

The second form also cannot skip or repeat a row when something is
inserted while the user is paging, which `OFFSET` does: a new transaction
pushes every later row one place down, so the next `OFFSET` page starts
one row too late.

The price is that the client can no longer ask for "page 7": it can only
ask for "the rows after this position". That position is what a cursor
holds - here the `(txn_date, statement_seq, id)` of the last row the
client received, which is exactly the sort key of `GET /transactions`.
(`statement_seq` joined in Phase 2 Step 4, ADR 014; a cursor from before
that has two parts and is refused, so the client starts again from page 1.)

Why base64, and what "opaque" means:

- It is ONE string the client passes back untouched, not two query
  parameters it has to keep in step with the sort order.
- It is deliberately not readable at a glance, so nobody is tempted to
  build one by hand or to depend on its contents. That keeps us free to
  change the sort key later (e.g. to add `amount` as a tie-breaker)
  without breaking any client.

It is NOT security: base64 is an encoding, not encryption, and anyone can
decode it. That is fine, because the contents are a date and an id of the
caller's OWN row. The endpoint still filters by `user_id`, so a cursor
copied from another user cannot reach their rows (rule 2) - it only moves
the reading position inside your own transactions.
"""

import base64
import uuid
from datetime import date

# Separates the parts inside the encoded string. An ISO date, an integer
# and a UUID cannot contain it, so splitting is unambiguous.
_SEPARATOR = "|"

# A position in the list: (txn_date, statement_seq, id).
CursorKey = tuple[date, int, uuid.UUID]


class CursorError(ValueError):
    """The cursor did not come from `encode_cursor`.

    A `ValueError` subclass, so a Pydantic validator can let it through as
    an ordinary validation failure (which becomes a 422 naming `cursor`).
    """


def encode_cursor(txn_date: date, statement_seq: int, row_id: uuid.UUID) -> str:
    """Turn the sort key of one row into the cursor string for the next page.

    `urlsafe_b64encode` uses `-` and `_` instead of `+` and `/`, so the
    result is safe in a query string. The trailing `=` padding is stripped
    for a tidier URL and put back when decoding.
    """
    raw = _SEPARATOR.join((txn_date.isoformat(), str(statement_seq), str(row_id)))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> CursorKey:
    """Read a cursor back into `(txn_date, statement_seq, id)`, or raise `CursorError`.

    Every failure mode is one error: not base64, not UTF-8 text, the wrong
    number of parts (an old two-part cursor included), not a date, not a
    positive whole number, not a UUID. The client cannot do anything
    different about any of them - the fix is always "send the `next_cursor`
    we gave you" - and the message never echoes what was sent.
    """
    # len % 4 == 1 can never be valid base64; the others need 0-3 "=" back.
    padding = "=" * (-len(cursor) % 4)
    try:
        # b64decode with altchars (not urlsafe_b64decode) because only this
        # form takes validate=True, which refuses characters outside the
        # alphabet instead of silently ignoring them - so a mangled cursor
        # is an error rather than a different reading position.
        raw = base64.b64decode(cursor + padding, altchars=b"-_", validate=True).decode()
    except ValueError:
        # binascii.Error (bad base64) and UnicodeDecodeError (bad UTF-8)
        # are both ValueError subclasses.
        raise CursorError(_MESSAGE) from None

    parts = raw.split(_SEPARATOR)
    if len(parts) != 3:
        raise CursorError(_MESSAGE)
    # Checked by hand, because int() is more forgiving than encode_cursor
    # ever is: it accepts "-5", " 5" and non-ASCII digits. Only plain ASCII
    # digits, not zero, are a seq this code wrote.
    seq_text = parts[1]
    if not (seq_text.isascii() and seq_text.isdigit()) or int(seq_text) == 0:
        raise CursorError(_MESSAGE)
    try:
        return date.fromisoformat(parts[0]), int(seq_text), uuid.UUID(parts[2])
    except ValueError:
        raise CursorError(_MESSAGE) from None


_MESSAGE = "is not a valid cursor; send the next_cursor from the previous page"
