"""Self-transfer matching: pair a debit with the credit it became (SPEC §8.4, ADR 018).

Paying your own credit card from savings, or moving money between two of
your bank accounts, shows up TWICE in your data: a debit in one account and
a credit in the other. Counted as spending and income, it would inflate
both. SPEC §8.4 asks for those pairs to be found and excluded:

    a debit in account A  +  a credit of the SAME amount in account B
    (same user, A != B), the credit 0-2 days after the debit

Pure, like the rest of the package: rows in, pairs out. Unlike the chain
(base.py), this module DOES see amounts and dates - matching is impossible
without them - but it only compares them and never sends them anywhere.

**One-to-one, nearest first, and never a guess.** Two debits of ₹5,000 on
the same day and one credit of ₹5,000 the next day: which one moved? There
is no way to tell, so neither is paired. The algorithm, gap by gap:

    for gap in 0, 1, 2 days:
        edges = (debit, credit) pairs still open, with the credit `gap`
                days after the debit
        an edge whose debit AND credit have no other edge at this gap
            -> a pair
        every other row with an edge at this gap
            -> closed, unmatched for good

A row that is ambiguous at its NEAREST gap is closed rather than tried
again at a larger one: its true partner is most likely one of the near
candidates, and pairing it with a farther row would be a confident wrong
answer. The result does not depend on the order the rows arrive in.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from app.parsers.base import Direction

# SPEC §8.4: "within 0-2 days". The credit may land on the same day as the
# debit or up to two days later (a transfer settling overnight, a card
# payment posted the next working day).
MAX_GAP_DAYS = 2


@dataclass(frozen=True)
class TransferCandidate:
    """The fields of one stored row that matching needs, and nothing else."""

    id: uuid.UUID
    account_id: uuid.UUID
    direction: Direction
    amount_paise: int
    txn_date: date


def match_self_transfers(
    rows: list[TransferCandidate],
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """The self-transfer pairs among one user's rows, as (debit id, credit id).

    The caller passes only rows that may be paired (never a manual one,
    never one a user rule decided - ADR 018) and only ONE user's rows.
    """
    debits = [row for row in rows if row.direction is Direction.DEBIT]
    # Credits grouped by amount, so each debit only looks at the few
    # credits that could possibly be its partner.
    credits_by_amount: dict[int, list[TransferCandidate]] = defaultdict(list)
    for row in rows:
        if row.direction is Direction.CREDIT:
            credits_by_amount[row.amount_paise].append(row)

    closed: set[uuid.UUID] = set()  # paired, or ambiguous at its nearest gap
    pairs: list[tuple[uuid.UUID, uuid.UUID]] = []
    for gap in range(MAX_GAP_DAYS + 1):
        edges = [
            (debit.id, credit.id)
            for debit in debits
            if debit.id not in closed
            for credit in credits_by_amount[debit.amount_paise]
            if credit.id not in closed
            and credit.account_id != debit.account_id
            and (credit.txn_date - debit.txn_date).days == gap
        ]
        edges_per_row: dict[uuid.UUID, int] = defaultdict(int)
        for debit_id, credit_id in edges:
            edges_per_row[debit_id] += 1
            edges_per_row[credit_id] += 1
        for debit_id, credit_id in edges:
            if edges_per_row[debit_id] == 1 and edges_per_row[credit_id] == 1:
                pairs.append((debit_id, credit_id))
        # Every row that had a candidate at this gap is now settled: paired
        # if its edge was the only one on both sides, ambiguous otherwise.
        closed.update(edges_per_row)
    return pairs
