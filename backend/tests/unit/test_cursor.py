"""The pagination cursor: round-trips, and refuses anything else.

A cursor is the only piece of our API the client sends back untouched, so
the two properties worth testing are "whatever we encode decodes to the
same key" and "anything else is a clean error, never a different reading
position".

Since Phase 2 Step 4 the key has three parts, `(txn_date, statement_seq,
id)` (ADR 014), and a cursor from before that - two parts - is refused.
"""

import base64
import datetime
import uuid

import pytest

from app.utils.cursor import CursorError, decode_cursor, encode_cursor

A_DATE = datetime.date(2026, 8, 4)
A_SEQ = 7
AN_ID = uuid.UUID("3f2504e0-4f89-41d3-9a0c-0305e82c3301")


@pytest.mark.parametrize(
    ("txn_date", "seq", "row_id"),
    [
        (A_DATE, A_SEQ, AN_ID),
        # Edge values: the first and last dates a DATE column holds in
        # practice, the smallest seq, and a seq far beyond any statement.
        (datetime.date(1, 1, 1), 1, uuid.UUID(int=0)),
        (datetime.date(9999, 12, 31), 2_147_483_647, uuid.UUID(int=(1 << 128) - 1)),
    ],
)
def test_a_cursor_round_trips(txn_date: datetime.date, seq: int, row_id: uuid.UUID) -> None:
    assert decode_cursor(encode_cursor(txn_date, seq, row_id)) == (txn_date, seq, row_id)


def test_a_cursor_is_url_safe_and_unpadded() -> None:
    """It travels in a query string, so no "+", "/" or "=" may appear."""
    cursor = encode_cursor(A_DATE, A_SEQ, AN_ID)

    assert not set(cursor) & set("+/=")


def test_every_part_of_the_key_changes_the_cursor() -> None:
    """Two different positions give two different cursors; one is stable."""
    cursor = encode_cursor(A_DATE, A_SEQ, AN_ID)

    assert cursor == encode_cursor(A_DATE, A_SEQ, AN_ID)
    assert cursor != encode_cursor(datetime.date(2026, 8, 5), A_SEQ, AN_ID)
    assert cursor != encode_cursor(A_DATE, A_SEQ + 1, AN_ID)
    assert cursor != encode_cursor(A_DATE, A_SEQ, uuid.UUID(int=1))


def _encode(raw: str) -> str:
    """Base64 some text by hand, to build cursors we never would."""
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    ("cursor", "why"),
    [
        ("", "empty"),
        ("!!!not base64!!!", "characters outside the base64 alphabet"),
        ("YQ", "valid base64, but not our three parts"),
        # The format before Phase 2 Step 4: a client holding one must start
        # again from the first page rather than land somewhere unexpected.
        (_encode(f"2026-08-04|{AN_ID}"), "an old two-part cursor"),
        (_encode(f"2026-08-04|7|{AN_ID}|extra"), "four parts"),
        (_encode(f"04/08/2026|7|{AN_ID}"), "the date is not ISO"),
        (_encode(f"2026-13-01|7|{AN_ID}"), "there is no month 13"),
        (_encode("2026-08-04|7|not-a-uuid"), "the id is not a UUID"),
        (_encode(f"2026-08-04|seven|{AN_ID}"), "the seq is not a number"),
        (_encode(f"2026-08-04|-7|{AN_ID}"), "a negative seq"),
        (_encode(f"2026-08-04|0|{AN_ID}"), "seq 0 (positions start at 1)"),
        (_encode(f"2026-08-04| 7|{AN_ID}"), "a padded seq int() would accept"),
        (_encode(f"2026-08-04|٧|{AN_ID}"), "a non-ASCII digit int() would accept"),
        # Truncated by a copy/paste accident.
        (encode_cursor(A_DATE, A_SEQ, AN_ID)[:-4], "cut short"),
    ],
)
def test_a_cursor_we_did_not_issue_is_rejected(cursor: str, why: str) -> None:
    with pytest.raises(CursorError):
        decode_cursor(cursor)


def test_the_error_never_echoes_the_cursor_back() -> None:
    """Error messages are shown to the user and can reach logs (rule 3)."""
    with pytest.raises(CursorError) as exc_info:
        decode_cursor("cGFzc3dvcmQ")

    assert "cGFzc3dvcmQ" not in str(exc_info.value)
    assert "next_cursor" in str(exc_info.value)
