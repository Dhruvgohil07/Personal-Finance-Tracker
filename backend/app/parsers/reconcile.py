"""Statement order and balance reconciliation (SPEC §6.4, Phase 2 Step 4).

Two small, pure jobs on a parsed statement's rows, before they are stored:

1. **`to_chronological`** puts the rows oldest-first. Banks export either
   way round (ICICI's net banking can sort newest-first), some even list
   the days oldest-first but each day's own transactions newest-first. The
   order is stored as `transactions.statement_seq`, so it must mean the
   same thing for every file: position in TIME, not position in the file.
2. **`reconcile`** checks the balance column: each printed balance must
   be the previous printed balance plus or minus everything in between,
   and opening + credits - debits must equal closing when the statement
   prints those. A row that breaks this means the parser misread something
   (or the bank's file is wrong), so the import is kept but marked
   `needs_review`, and the rows are listed by their row number in the file.

"Pure" in the same sense as the rest of `app/parsers/`: no database, no
HTTP, no logging - plain data in, plain data out, so every case is a
two-line unit test.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import groupby

from app.parsers.base import RawRow


class IssueKind(StrEnum):
    """What looked wrong. Stored as text in `reconciliation_issues`."""

    # the previous printed balance +/- every amount since != this balance
    BALANCE_MISMATCH = "balance_mismatch"
    # the opening balance +/- every amount up to here != this row's balance
    # (the first row with a balance, checked against a printed opening)
    OPENING_BALANCE_MISMATCH = "opening_balance_mismatch"
    # opening + credits - debits != closing (about the whole statement)
    CLOSING_BALANCE_MISMATCH = "closing_balance_mismatch"


@dataclass(frozen=True)
class Issue:
    """One thing that does not add up.

    `row_number` is the row in the FILE (1-based, as `RawRow` carries it),
    so the user can find it in the statement. It is None for an issue about
    the statement as a whole (the closing balance). Deliberately no amounts
    or balances: the issue is stored and returned by the API, and the row
    number is enough to find the row (CLAUDE.md rule 3).
    """

    kind: IssueKind
    row_number: int | None = None


def to_chronological(rows: Sequence[RawRow]) -> list[RawRow]:
    """The rows oldest-first, otherwise in the file's own order.

    Rows are only ever kept as they are or REVERSED, never sorted. Sorting
    by date would scramble the order within a day, which is exactly the
    information the file has and nothing else does (no time of day is
    printed). Two decisions, in this order:

    1. **The days.** Dates that never go up (and do change) mean a
       newest-first file: the whole list is reversed. Dates that go both up
       and down mean the file has no usable order at all (sorted by amount,
       say): it is returned as it is, and `reconcile` will say so.
    2. **Each day.** Within one day the dates say nothing, so the balance
       chain decides: the day's rows are reversed if more balance links
       hold that way, counting the link from the previous day's last row.
       This handles a file whose days run oldest-first but whose
       transactions within a day run newest-first, and a statement of a
       single day. A day whose balances cannot tell (no balances, or a tie)
       keeps the file's order.

    The function returns a new list; the statement is not modified.
    """
    ordered = list(rows)
    dates = [row.txn_date for row in ordered]
    pairs = list(zip(dates, dates[1:], strict=False))
    ascending = all(earlier <= later for earlier, later in pairs)
    descending = all(earlier >= later for earlier, later in pairs)
    if descending and not ascending:
        # Newest-first. (Both true means a single day: step 2 decides.)
        ordered.reverse()
    elif not ascending:
        return ordered

    result: list[RawRow] = []
    for _day, group in groupby(ordered, key=lambda row: row.txn_date):
        day = list(group)
        # The previous day's last row, if any: the link into this day.
        before = result[-1:]
        if _links_that_hold(before + day[::-1]) > _links_that_hold(before + day):
            day.reverse()
        result.extend(day)
    return result


def reconcile(
    rows: Sequence[RawRow],
    *,
    opening_balance_paise: int | None = None,
    closing_balance_paise: int | None = None,
) -> list[Issue] | None:
    """Check the balances of CHRONOLOGICAL rows (see `to_chronological`).

    Returns None when there is nothing to check (no row has a balance - a
    bare CSV without a balance column), and otherwise the list of issues,
    which is empty for a statement that adds up. The difference matters to
    the user: "checked, all fine" is not the same as "could not check".

    **Rows without a balance are not errors.** Many statements print the
    balance only once per day, or leave some cells blank. Their amounts are
    carried forward and checked at the next printed balance: "the last
    printed balance, plus or minus everything since, must equal this one".
    So an issue on a row means "something between the previous printed
    balance and this row is wrong" - and a misread amount inside such a gap
    is still caught.

    Each printed balance is checked against the PRINTED balance before it,
    not against a running total from the start. So one misread value points
    at its own spot instead of making every later row wrong.

    The opening and closing balances are checked only when the statement
    prints them (`ParsedStatement` carries them only then).
    """
    if not any(row.balance_after_paise is not None for row in rows):
        return None

    issues = [
        Issue(kind, row.row_number)
        for kind, row, expected in _balance_checks(rows, opening_balance_paise)
        if expected != row.balance_after_paise
    ]

    if opening_balance_paise is not None and closing_balance_paise is not None:
        total = sum(row.signed_paise for row in rows)
        if opening_balance_paise + total != closing_balance_paise:
            issues.append(Issue(IssueKind.CLOSING_BALANCE_MISMATCH))

    return issues


def _balance_checks(
    rows: Sequence[RawRow], opening_balance_paise: int | None
) -> Iterator[tuple[IssueKind, RawRow, int]]:
    """Every row with a printed balance that can be checked, with what it should be.

    Yields `(kind, row, expected balance)`. The reference is the last
    printed balance (or the opening balance, until the first one is seen),
    plus the signed amounts of every row since - the rows without a
    balance included. Without a reference (no opening balance, before the
    first printed balance), a row cannot be checked and is not yielded.
    """
    reference = opening_balance_paise
    kind = IssueKind.OPENING_BALANCE_MISMATCH
    carried = 0
    for row in rows:
        carried += row.signed_paise
        if row.balance_after_paise is None:
            continue
        if reference is not None:
            yield kind, row, reference + carried
        # From here on, rows are checked against this printed balance.
        reference, kind, carried = row.balance_after_paise, IssueKind.BALANCE_MISMATCH, 0


def _links_that_hold(rows: Sequence[RawRow]) -> int:
    """How many consecutive pairs agree with each other's balances, in this order."""
    return sum(
        1
        for before, after in zip(rows, rows[1:], strict=False)
        if before.balance_after_paise is not None
        and after.balance_after_paise is not None
        and before.balance_after_paise + after.signed_paise == after.balance_after_paise
    )
