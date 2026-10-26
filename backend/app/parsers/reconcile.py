"""Statement order and balance reconciliation (SPEC §6.4, Phase 2 Step 4).

Two small, pure jobs on a parsed statement's rows, before they are stored:

1. **`to_chronological`** puts the rows oldest-first. Banks export either
   way round (ICICI's net banking can sort newest-first), and the order is
   stored as `transactions.statement_seq`, so it must mean the same thing
   for every file: position in TIME, not position in the file.
2. **`reconcile`** checks the balance column: every row's balance must be
   the previous balance plus or minus that row's amount, and opening +
   credits - debits must equal closing. A row that breaks this means the
   parser misread something (or the bank's file is wrong), so the import
   is kept but marked `needs_review`, and the rows are listed by their
   row number in the file.

"Pure" in the same sense as the rest of `app/parsers/`: no database, no
HTTP, no logging - plain data in, plain data out, so every case is a
two-line unit test.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.parsers.base import Direction, RawRow


class IssueKind(StrEnum):
    """What looked wrong. Stored as text in `reconciliation_issues`."""

    # previous balance +/- this row's amount != this row's balance
    BALANCE_MISMATCH = "balance_mismatch"
    # the statement has a balance column, but this row has no balance
    MISSING_BALANCE = "missing_balance"
    # opening balance +/- the first row's amount != the first row's balance
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
    printed). The decision:

    - dates never go down (oldest-first)             -> kept
    - dates never go up, and do change (newest-first) -> reversed
    - every row on the same day                      -> the balance chain
      decides: whichever direction more rows' balances agree with
    - dates go both up and down                      -> kept as they are;
      such a file has no usable order, and `reconcile` will say so

    The function returns a new list; the statement is not modified.
    """
    ordered = list(rows)
    if len(ordered) < 2:
        return ordered

    dates = [row.txn_date for row in ordered]
    ascending = all(earlier <= later for earlier, later in zip(dates, dates[1:], strict=False))
    descending = all(earlier >= later for earlier, later in zip(dates, dates[1:], strict=False))

    if ascending and descending:
        # One single day: only the balances can tell the order.
        if _links_that_hold(ordered[::-1]) > _links_that_hold(ordered):
            ordered.reverse()
    elif descending:
        ordered.reverse()
    return ordered


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

    Each row is checked against the balance actually PRINTED on the row
    before it, not against a running total. So one misread amount gives one
    issue on its own row, instead of every row after it being wrong too.

    The opening and closing balances are checked only when the parser knows
    them. (ICICI prints neither and the parser derives both from the rows,
    so for ICICI these two checks can only fail if a link is broken too.)
    """
    if not any(row.balance_after_paise is not None for row in rows):
        return None

    issues: list[Issue] = []
    previous: int | None = opening_balance_paise
    first = True
    for row in rows:
        if row.balance_after_paise is None:
            issues.append(Issue(IssueKind.MISSING_BALANCE, row.row_number))
        elif previous is not None and previous + _signed(row) != row.balance_after_paise:
            kind = IssueKind.OPENING_BALANCE_MISMATCH if first else IssueKind.BALANCE_MISMATCH
            issues.append(Issue(kind, row.row_number))
        # A row without a balance breaks the chain: the next row has
        # nothing printed to be checked against, so it is not checked.
        previous = row.balance_after_paise
        first = False

    if opening_balance_paise is not None and closing_balance_paise is not None:
        total = sum(_signed(row) for row in rows)
        if opening_balance_paise + total != closing_balance_paise:
            issues.append(Issue(IssueKind.CLOSING_BALANCE_MISMATCH))

    return issues


def _signed(row: RawRow) -> int:
    """The row's effect on the balance: minus for a debit, plus for a credit."""
    return -row.amount_paise if row.direction is Direction.DEBIT else row.amount_paise


def _links_that_hold(rows: Sequence[RawRow]) -> int:
    """How many consecutive pairs agree with each other's balances, in this order."""
    return sum(
        1
        for before, after in zip(rows, rows[1:], strict=False)
        if before.balance_after_paise is not None
        and after.balance_after_paise is not None
        and before.balance_after_paise + _signed(after) == after.balance_after_paise
    )
