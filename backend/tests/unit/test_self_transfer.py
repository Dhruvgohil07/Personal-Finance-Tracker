"""Tests for app.categorizer.self_transfer: which debits and credits pair up.

Rows are built by hand: an account letter, a direction, an amount in
rupees and a day of August 2026. Ids are fixed so a failing assertion
names the rows.
"""

import itertools
import uuid
from datetime import date

import pytest

from app.categorizer.self_transfer import MAX_GAP_DAYS, TransferCandidate, match_self_transfers
from app.parsers.base import Direction

ACCOUNTS = {name: uuid.uuid4() for name in "ABC"}


def row(name: str, account: str, direction: str, rupees: int, day: int) -> TransferCandidate:
    return TransferCandidate(
        id=uuid.uuid5(uuid.NAMESPACE_URL, name),
        account_id=ACCOUNTS[account],
        direction=Direction.DEBIT if direction == "dr" else Direction.CREDIT,
        amount_paise=rupees * 100,
        txn_date=date(2026, 8, day),
    )


def ids(*names: str) -> tuple[uuid.UUID, ...]:
    return tuple(uuid.uuid5(uuid.NAMESPACE_URL, name) for name in names)


def test_a_savings_to_credit_card_payment_is_a_pair() -> None:
    debit = row("pay", "A", "dr", 12_500, 10)
    credit = row("card", "B", "cr", 12_500, 11)

    assert match_self_transfers([debit, credit]) == [ids("pay", "card")]


@pytest.mark.parametrize("gap", range(MAX_GAP_DAYS + 1))
def test_the_credit_may_come_0_to_2_days_later(gap: int) -> None:
    rows = [row("d", "A", "dr", 500, 10), row("c", "B", "cr", 500, 10 + gap)]

    assert match_self_transfers(rows) == [ids("d", "c")]


@pytest.mark.parametrize(
    "credit",
    [
        row("c", "B", "cr", 500, 13),  # 3 days later: too far
        row("c", "B", "cr", 500, 9),  # the day BEFORE the debit
        row("c", "A", "cr", 500, 10),  # the same account: a refund, not a transfer
        row("c", "B", "cr", 501, 10),  # a different amount
        row("c", "B", "dr", 500, 10),  # two debits are not a transfer
    ],
    ids=["three_days", "before", "same_account", "other_amount", "both_debits"],
)
def test_what_is_not_a_pair(credit: TransferCandidate) -> None:
    assert match_self_transfers([row("d", "A", "dr", 500, 10), credit]) == []


def test_the_nearest_credit_wins() -> None:
    rows = [
        row("d", "A", "dr", 500, 10),
        row("later", "B", "cr", 500, 12),
        row("next", "C", "cr", 500, 11),
    ]

    assert match_self_transfers(rows) == [ids("d", "next")]


def test_two_equally_near_credits_leave_the_debit_unmatched() -> None:
    rows = [
        row("d", "A", "dr", 500, 10),
        row("c1", "B", "cr", 500, 11),
        row("c2", "C", "cr", 500, 11),
    ]

    assert match_self_transfers(rows) == []


def test_two_equally_near_debits_leave_the_credit_unmatched() -> None:
    rows = [
        row("d1", "A", "dr", 500, 10),
        row("d2", "C", "dr", 500, 10),
        row("c", "B", "cr", 500, 10),
    ]

    assert match_self_transfers(rows) == []


def test_a_row_ambiguous_at_its_nearest_gap_is_not_tried_farther_away() -> None:
    """d1 has two candidates the same day; the credit two days later must
    not be handed to it just because the near ones were ambiguous."""
    rows = [
        row("d1", "A", "dr", 500, 10),
        row("c1", "B", "cr", 500, 10),
        row("c2", "C", "cr", 500, 10),
        row("far", "B", "cr", 500, 12),
    ]

    assert match_self_transfers(rows) == []


def test_a_taken_credit_is_not_paired_twice() -> None:
    """One-to-one: d2 is a day farther from the credit than d1, so d1 wins
    and d2 stays unmatched."""
    rows = [
        row("d1", "A", "dr", 500, 11),
        row("d2", "C", "dr", 500, 10),
        row("c", "B", "cr", 500, 11),
    ]

    assert match_self_transfers(rows) == [ids("d1", "c")]


def test_independent_pairs_are_all_found() -> None:
    rows = [
        row("rent_out", "A", "dr", 15_000, 1),
        row("rent_in", "B", "cr", 15_000, 1),
        row("card_pay", "A", "dr", 8_000, 5),
        row("card_in", "C", "cr", 8_000, 6),
        row("shop", "A", "dr", 300, 5),  # spending: no partner
    ]

    assert sorted(match_self_transfers(rows)) == sorted(
        [ids("rent_out", "rent_in"), ids("card_pay", "card_in")]
    )


def test_the_result_does_not_depend_on_row_order() -> None:
    rows = [
        row("d1", "A", "dr", 500, 10),
        row("c1", "B", "cr", 500, 10),
        row("d2", "A", "dr", 500, 12),
        row("c2", "B", "cr", 500, 13),
        row("d3", "C", "dr", 700, 1),
        row("c3", "A", "cr", 700, 2),
    ]
    expected = sorted(match_self_transfers(rows))

    for order in itertools.permutations(rows):
        assert sorted(match_self_transfers(list(order))) == expected
    assert expected == sorted([ids("d1", "c1"), ids("d2", "c2"), ids("d3", "c3")])


def test_no_rows_no_pairs() -> None:
    assert match_self_transfers([]) == []
