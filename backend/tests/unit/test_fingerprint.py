"""Tests for app.parsers.fingerprint: the dedupe key (SPEC §6.3).

Two properties matter, and every test below is one of them:

- **Stable**: the same transaction always hashes to the same value, so
  re-uploading or overlapping statements do not create duplicates.
- **Sensitive**: if any identifying field differs, the hash differs, so two
  genuinely different transactions are never collapsed into one.
"""

import uuid
from datetime import date

from app.parsers.base import Direction, NormalizedRow, RawRow
from app.parsers.fingerprint import (
    assign_occurrence_indexes,
    compute_fingerprint,
    fingerprints_for,
)

ACCOUNT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_ACCOUNT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def make_row(
    *,
    txn_date: date = date(2026, 8, 14),
    amount_paise: int = 2000,
    direction: Direction = Direction.DEBIT,
    description: str = "UPI/ /CHAI POINT",
    balance_after_paise: int | None = None,
    value_date: date | None = None,
    row_number: int = 1,
) -> NormalizedRow:
    """A normalized row with one field changed per test."""
    raw = RawRow(
        row_number=row_number,
        txn_date=txn_date,
        amount_paise=amount_paise,
        direction=direction,
        raw_description=description,
        value_date=value_date,
        balance_after_paise=balance_after_paise,
    )
    return NormalizedRow(raw=raw, normalized_description=description, merchant_key=None)


# --- compute_fingerprint: shape and stability ----------------------------


def test_fingerprint_is_a_sha256_hex_string() -> None:
    fingerprint = compute_fingerprint(ACCOUNT_ID, make_row(), 0)

    assert len(fingerprint) == 64
    assert all(character in "0123456789abcdef" for character in fingerprint)


def test_same_row_gives_the_same_fingerprint() -> None:
    # The whole point: a transaction re-read from an overlapping statement
    # hashes identically, so ON CONFLICT DO NOTHING skips it (Step 7).
    assert compute_fingerprint(ACCOUNT_ID, make_row(), 0) == compute_fingerprint(
        ACCOUNT_ID, make_row(), 0
    )


def test_a_uuid_and_its_string_form_agree() -> None:
    # Step 7 may hold the account id as a UUID or as text depending on
    # where it came from; both must produce the same key.
    assert compute_fingerprint(ACCOUNT_ID, make_row(), 0) == compute_fingerprint(
        str(ACCOUNT_ID), make_row(), 0
    )


# --- compute_fingerprint: sensitivity to every field ---------------------


def test_a_different_account_gives_a_different_fingerprint() -> None:
    # A transfer between the user's own accounts appears in both
    # statements, and those are two real rows.
    assert compute_fingerprint(ACCOUNT_ID, make_row(), 0) != compute_fingerprint(
        OTHER_ACCOUNT_ID, make_row(), 0
    )


def test_each_identifying_field_changes_the_fingerprint() -> None:
    baseline = compute_fingerprint(ACCOUNT_ID, make_row(balance_after_paise=5000000), 0)

    variants = {
        "date": make_row(txn_date=date(2026, 8, 15), balance_after_paise=5000000),
        "amount": make_row(amount_paise=2001, balance_after_paise=5000000),
        "direction": make_row(direction=Direction.CREDIT, balance_after_paise=5000000),
        "description": make_row(description="UPI/ /CHAI PLACE", balance_after_paise=5000000),
        "balance": make_row(balance_after_paise=5000001),
    }

    for name, row in variants.items():
        assert compute_fingerprint(ACCOUNT_ID, row, 0) != baseline, name


def test_occurrence_index_changes_the_fingerprint() -> None:
    row = make_row()

    assert compute_fingerprint(ACCOUNT_ID, row, 0) != compute_fingerprint(ACCOUNT_ID, row, 1)


def test_value_date_is_not_part_of_the_fingerprint() -> None:
    # Some statements print a value date and some do not. The same
    # transaction must hash the same either way, so it is excluded.
    with_value_date = make_row(value_date=date(2026, 8, 15))
    without = make_row()

    assert compute_fingerprint(ACCOUNT_ID, with_value_date, 0) == compute_fingerprint(
        ACCOUNT_ID, without, 0
    )


def test_missing_balance_is_not_the_text_none() -> None:
    # A missing balance becomes an empty field. If it were formatted as
    # "None", a row whose balance column literally read "None" would
    # collide with a row that had no balance at all.
    no_balance = compute_fingerprint(ACCOUNT_ID, make_row(balance_after_paise=None), 0)
    text_none = compute_fingerprint(ACCOUNT_ID, make_row(description="UPI/ /CHAI POINT|None"), 0)

    assert no_balance != text_none


def test_fields_cannot_shift_across_the_separator() -> None:
    # The separator is "|", and `normalize_description` removes pipes from
    # narrations. These two rows differ only in where a pipe-ish boundary
    # would fall, and must not collide.
    first = make_row(description="ACME STORES", balance_after_paise=100)
    second = make_row(description="ACME STORES 100", balance_after_paise=None)

    assert compute_fingerprint(ACCOUNT_ID, first, 0) != compute_fingerprint(ACCOUNT_ID, second, 0)


# --- assign_occurrence_indexes -------------------------------------------


def test_identical_rows_are_numbered_in_order() -> None:
    # Two ₹20 chais and a bus ticket on the same day, no balance column:
    # the two chais are indistinguishable, so they get 0 and 1 and both
    # survive the import.
    rows = [
        make_row(description="CHAI POINT", row_number=1),
        make_row(description="BUS TICKET", amount_paise=3000, row_number=2),
        make_row(description="CHAI POINT", row_number=3),
    ]

    assert assign_occurrence_indexes(rows) == [0, 0, 1]


def test_rows_differing_in_any_field_all_start_at_zero() -> None:
    rows = [
        make_row(),
        make_row(amount_paise=3000),
        make_row(txn_date=date(2026, 8, 15)),
        make_row(direction=Direction.CREDIT),
        make_row(balance_after_paise=1),
    ]

    assert assign_occurrence_indexes(rows) == [0, 0, 0, 0, 0]


def test_a_balance_column_tells_identical_looking_rows_apart() -> None:
    # With balances present, two equal payments on one day are already
    # distinct, so neither needs a non-zero index.
    rows = [
        make_row(balance_after_paise=5000000),
        make_row(balance_after_paise=4998000),
    ]

    assert assign_occurrence_indexes(rows) == [0, 0]


def test_no_rows_gives_no_indexes() -> None:
    assert assign_occurrence_indexes([]) == []


# --- fingerprints_for: the whole-statement helper ------------------------


def test_fingerprints_for_matches_manual_index_assignment() -> None:
    rows = [
        make_row(description="CHAI POINT", row_number=1),
        make_row(description="CHAI POINT", row_number=2),
    ]

    assert fingerprints_for(ACCOUNT_ID, rows) == [
        compute_fingerprint(ACCOUNT_ID, rows[0], 0),
        compute_fingerprint(ACCOUNT_ID, rows[1], 1),
    ]


def test_duplicate_rows_within_one_statement_stay_distinct() -> None:
    rows = [make_row(description="CHAI POINT"), make_row(description="CHAI POINT")]

    fingerprints = fingerprints_for(ACCOUNT_ID, rows)

    assert len(set(fingerprints)) == 2


def test_overlapping_statements_produce_the_same_fingerprints() -> None:
    # The scenario from SPEC §6.3: 1-31 August, then 15 August-15 September.
    # The overlap must hash identically in both imports so only the new
    # rows are inserted the second time.
    early = make_row(txn_date=date(2026, 8, 2), description="RENT", amount_paise=2500000)
    overlap_first = make_row(txn_date=date(2026, 8, 20), description="CHAI POINT")
    overlap_second = make_row(txn_date=date(2026, 8, 20), description="CHAI POINT")
    late = make_row(txn_date=date(2026, 9, 10), description="SALARY", direction=Direction.CREDIT)

    august = fingerprints_for(ACCOUNT_ID, [early, overlap_first, overlap_second])
    september = fingerprints_for(ACCOUNT_ID, [overlap_first, overlap_second, late])

    # Both copies of the twice-repeated row match, in the same order.
    assert august[1:] == september[:2]
    # Only the September-only row is new.
    assert set(september) - set(august) == {september[2]}
