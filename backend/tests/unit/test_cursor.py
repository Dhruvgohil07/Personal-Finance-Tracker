"""The pagination cursor: round-trips, and refuses anything else.

A cursor is the only piece of our API the client sends back untouched, so
the two properties worth testing are "whatever we encode decodes to the
same key" and "anything else is a clean error, never a different reading
position".
"""

import base64
import datetime
import uuid

import pytest

from app.utils.cursor import CursorError, decode_cursor, encode_cursor

A_DATE = datetime.date(2026, 8, 4)
AN_ID = uuid.UUID("3f2504e0-4f89-41d3-9a0c-0305e82c3301")


@pytest.mark.parametrize(
    ("txn_date", "row_id"),
    [
        (A_DATE, AN_ID),
        # Edge dates: the first and last a DATE column can hold in practice.
        (datetime.date(1, 1, 1), uuid.UUID(int=0)),
        (datetime.date(9999, 12, 31), uuid.UUID(int=(1 << 128) - 1)),
    ],
)
def test_a_cursor_round_trips(txn_date: datetime.date, row_id: uuid.UUID) -> None:
    assert decode_cursor(encode_cursor(txn_date, row_id)) == (txn_date, row_id)


def test_a_cursor_is_url_safe_and_unpadded() -> None:
    """It travels in a query string, so no "+", "/" or "=" may appear."""
    cursor = encode_cursor(A_DATE, AN_ID)

    assert not set(cursor) & set("+/=")


def test_the_cursor_does_not_depend_on_the_row_order() -> None:
    """Two different rows give two different cursors, and one row is stable."""
    assert encode_cursor(A_DATE, AN_ID) == encode_cursor(A_DATE, AN_ID)
    assert encode_cursor(A_DATE, AN_ID) != encode_cursor(A_DATE, uuid.UUID(int=1))
    assert encode_cursor(A_DATE, AN_ID) != encode_cursor(datetime.date(2026, 8, 5), AN_ID)


def _encode(raw: str) -> str:
    """Base64 some text by hand, to build cursors we never would."""
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    ("cursor", "why"),
    [
        ("", "empty"),
        ("!!!not base64!!!", "characters outside the base64 alphabet"),
        ("YQ", "valid base64, but not our two parts"),
        (_encode("2026-08-04"), "the id is missing"),
        (_encode("2026-08-04|an-id|extra"), "three parts"),
        (_encode("04/08/2026|" + str(AN_ID)), "the date is not ISO"),
        (_encode("2026-08-04|not-a-uuid"), "the id is not a UUID"),
        (_encode("2026-13-01|" + str(AN_ID)), "there is no month 13"),
        # Truncated by a copy/paste accident.
        (encode_cursor(A_DATE, AN_ID)[:-4], "cut short"),
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


def test_a_cursor_error_is_a_value_error() -> None:
    """So a Pydantic validator can let it through as a plain 422."""
    assert issubclass(CursorError, ValueError)
