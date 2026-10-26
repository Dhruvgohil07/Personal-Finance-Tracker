"""Statement order and balance reconciliation (app/parsers/reconcile.py).

The rows are built by hand with a running balance, so every test can say
exactly which balance is wrong and where. Amounts are integer paise
throughout (ADR 001).
"""

from datetime import date

from app.parsers.base import Direction, RawRow
from app.parsers.reconcile import Issue, IssueKind, reconcile, to_chronological

DEBIT = Direction.DEBIT
CREDIT = Direction.CREDIT
OPENING = 100_000  # ₹1,000.00


def make_rows(
    *moves: tuple[int, Direction, int], opening: int = OPENING, balances: bool = True
) -> list[RawRow]:
    """Rows from (day of August, direction, amount), with correct balances.

    Row numbers start at 10, like a real file whose table starts after a
    header, so a test can tell a row NUMBER from a list INDEX.
    """
    rows = []
    balance = opening
    for index, (day, direction, amount) in enumerate(moves):
        balance += -amount if direction is DEBIT else amount
        rows.append(
            RawRow(
                row_number=10 + index,
                txn_date=date(2026, 8, day),
                amount_paise=amount,
                direction=direction,
                raw_description=f"ROW {index}",
                balance_after_paise=balance if balances else None,
            )
        )
    return rows


def with_balance(row: RawRow, balance: int | None) -> RawRow:
    """The same row with a different printed balance (a misread, say)."""
    return RawRow(
        row_number=row.row_number,
        txn_date=row.txn_date,
        amount_paise=row.amount_paise,
        direction=row.direction,
        raw_description=row.raw_description,
        balance_after_paise=balance,
    )


THREE_DAYS = make_rows((1, DEBIT, 7_000), (2, CREDIT, 50_000), (3, DEBIT, 2_000))


# --- reconcile --------------------------------------------------------------


def test_a_statement_that_adds_up_has_no_issues() -> None:
    closing = THREE_DAYS[-1].balance_after_paise

    assert reconcile(THREE_DAYS, opening_balance_paise=OPENING, closing_balance_paise=closing) == []


def test_one_misread_balance_is_one_issue_on_its_own_row() -> None:
    """The next row is checked against the balance PRINTED before it, so a
    single bad balance does not make every later row look wrong too...
    except the row right after it, whose link to the bad value breaks as
    well. Two issues, both pointing at the spot."""
    rows = list(THREE_DAYS)
    rows[1] = with_balance(rows[1], 999)

    assert reconcile(rows) == [
        Issue(IssueKind.BALANCE_MISMATCH, 11),
        Issue(IssueKind.BALANCE_MISMATCH, 12),
    ]


def test_a_misread_amount_is_one_issue() -> None:
    """A wrong AMOUNT with the right balances breaks only its own link."""
    rows = list(THREE_DAYS)
    misread = rows[1]
    rows[1] = RawRow(
        row_number=misread.row_number,
        txn_date=misread.txn_date,
        amount_paise=5_000,  # the statement says 50_000
        direction=misread.direction,
        raw_description=misread.raw_description,
        balance_after_paise=misread.balance_after_paise,
    )

    assert reconcile(rows) == [Issue(IssueKind.BALANCE_MISMATCH, 11)]


def test_a_missing_row_breaks_the_link_after_it() -> None:
    """A row the parser skipped: the next row no longer follows from the
    one before. That is the parser bug §6.4 exists to catch."""
    rows = [THREE_DAYS[0], THREE_DAYS[2]]

    assert reconcile(rows) == [Issue(IssueKind.BALANCE_MISMATCH, 12)]


def test_no_balance_column_means_nothing_was_checked() -> None:
    """None, not []: "could not check" is not "checked, all fine"."""
    rows = make_rows((1, DEBIT, 7_000), (2, CREDIT, 50_000), balances=False)

    assert reconcile(rows) is None
    assert reconcile([]) is None


def test_a_row_without_a_balance_in_a_statement_with_balances() -> None:
    """Reported, and the row after it is not checked (nothing to check
    it against) rather than reported as a second, made-up mismatch."""
    rows = list(THREE_DAYS)
    rows[1] = with_balance(rows[1], None)

    assert reconcile(rows) == [Issue(IssueKind.MISSING_BALANCE, 11)]


def test_the_first_row_is_checked_against_the_opening_balance() -> None:
    assert reconcile(THREE_DAYS, opening_balance_paise=OPENING + 1) == [
        Issue(IssueKind.OPENING_BALANCE_MISMATCH, 10)
    ]


def test_without_an_opening_balance_the_first_row_is_not_checked() -> None:
    rows = list(THREE_DAYS)
    rows[0] = with_balance(rows[0], 1)  # cannot be checked against anything

    assert reconcile(rows) == [Issue(IssueKind.BALANCE_MISMATCH, 11)]


def test_a_closing_balance_that_does_not_add_up() -> None:
    """About the statement as a whole, so it has no row number."""
    closing = THREE_DAYS[-1].balance_after_paise
    assert closing is not None

    issues = reconcile(THREE_DAYS, opening_balance_paise=OPENING, closing_balance_paise=closing + 1)

    assert issues == [Issue(IssueKind.CLOSING_BALANCE_MISMATCH)]


def test_an_overdraft_balance_is_fine() -> None:
    """Balances may go negative (an overdrawn account or a credit card)."""
    rows = make_rows((1, DEBIT, 150_000), (2, CREDIT, 10_000))

    assert rows[0].balance_after_paise is not None
    assert rows[0].balance_after_paise < 0
    assert reconcile(rows) == []


# --- to_chronological -------------------------------------------------------


def test_an_oldest_first_statement_is_kept_as_it_is() -> None:
    assert to_chronological(THREE_DAYS) == THREE_DAYS


def test_a_newest_first_statement_is_reversed() -> None:
    newest_first = list(reversed(THREE_DAYS))

    assert to_chronological(newest_first) == THREE_DAYS
    # And then it reconciles: the chain only works oldest-first.
    assert reconcile(to_chronological(newest_first)) == []


def test_days_with_several_rows_keep_their_order_within_the_day() -> None:
    """Never sorted, only reversed: within a day the file's order is the
    only order there is."""
    rows = make_rows((1, DEBIT, 100), (1, DEBIT, 200), (2, DEBIT, 300), (2, DEBIT, 400))

    assert to_chronological(rows) == rows
    assert to_chronological(rows[::-1]) == rows


def test_a_single_day_statement_is_ordered_by_its_balances() -> None:
    """Every row on one day: the dates say nothing, so the balance chain
    decides which way round the file is."""
    one_day = make_rows((5, DEBIT, 100), (5, CREDIT, 2_500), (5, DEBIT, 300))

    assert to_chronological(one_day) == one_day
    assert to_chronological(one_day[::-1]) == one_day


def test_a_single_day_statement_without_balances_keeps_the_file_order() -> None:
    one_day = make_rows((5, DEBIT, 100), (5, DEBIT, 200), balances=False)

    assert to_chronological(one_day[::-1]) == one_day[::-1]


def test_a_statement_with_no_order_is_kept_and_reconcile_says_so() -> None:
    """Dates going up and down (e.g. sorted by amount): nothing to reverse,
    and the balance chain breaks, so the import ends in needs_review."""
    a, b, c = THREE_DAYS
    shuffled = [b, a, c]

    assert to_chronological(shuffled) == shuffled
    assert reconcile(shuffled)


def test_short_statements_are_returned_as_they_are() -> None:
    assert to_chronological([]) == []
    assert to_chronological(THREE_DAYS[:1]) == THREE_DAYS[:1]


def test_the_input_list_is_not_modified() -> None:
    newest_first = list(reversed(THREE_DAYS))
    copy = list(newest_first)

    to_chronological(newest_first)

    assert newest_first == copy
