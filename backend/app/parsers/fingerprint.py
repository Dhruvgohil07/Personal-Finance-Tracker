"""The per-row dedupe key that makes imports idempotent (SPEC §6.3).

The problem: statements overlap. A user uploads 1-31 August, then later
15 August - 15 September. Two weeks of transactions are in both files, and
re-importing them must not create duplicate rows (CLAUDE.md rule 5).

Two levels of protection:

- Level 1, the whole file: `statement_uploads` has `unique(user_id,
  file_sha256)`, so uploading the *exact same file* twice is rejected
  immediately (Step 7).
- Level 2, each row: this module. Every transaction gets a `fingerprint` -
  a sha256 of the fields that identify it - and `transactions` has
  `unique(account_id, fingerprint)`. Step 7 inserts with
  `ON CONFLICT DO NOTHING`, so rows already present are skipped and
  counted as duplicates while genuinely new rows go in. One SQL statement,
  no "does this row exist?" query per row, and it stays correct even if two
  imports run at the same time, because the database decides.

The fields (exactly as SPEC §6.3 lists them):

    account_id | txn_date | amount_paise | direction |
    normalized_description | balance_after_paise | occurrence_index

`occurrence_index` is what makes two genuinely separate but identical
payments survive - see `assign_occurrence_indexes`.

Why a hash and not a unique constraint over those seven columns directly?
A single `TEXT` column is one small b-tree index instead of a wide
multi-column one, it keeps `NULL` out of the key (`balance_after_paise` is
often missing, and in SQL `NULL != NULL`, which would break the
constraint), and the fingerprint is computed once in Python where it is
unit tested, rather than depending on how Postgres compares seven values.
"""

import hashlib
import uuid
from collections.abc import Sequence

from app.parsers.base import NormalizedRow

# SPEC §6.3 writes the fingerprint input with "|" between the fields.
# `normalize_description` removes pipes from narrations (see the note
# there), so this separator cannot appear inside a field and the field
# boundaries stay unambiguous.
_SEPARATOR = "|"


def _identity(row: NormalizedRow) -> tuple[object, ...]:
    """The fields that decide whether two rows look identical.

    Everything that goes into the fingerprint except `account_id` (the
    same for the whole import) and `occurrence_index` (what we are about to
    compute). `value_date` is deliberately NOT here: some statements print
    it and some do not, and the same transaction must fingerprint the same
    either way.
    """
    return (
        row.raw.txn_date,
        row.raw.amount_paise,
        row.raw.direction,
        row.normalized_description,
        row.raw.balance_after_paise,
    )


def assign_occurrence_indexes(rows: Sequence[NormalizedRow]) -> list[int]:
    """Number identical rows 0, 1, 2, ... in statement order.

    Two cups of chai at the same shop for ₹20 on the same day produce two
    statement rows that are identical in every field we fingerprint - same
    date, same amount, same narration - and, if the statement has no
    balance column, nothing at all tells them apart. Without this index
    they would hash to the same value and the second one would be dropped
    as a "duplicate", losing a real ₹20.

    So the first such row gets index 0, the second 1, and so on:

        [chai ₹20, bus ₹30, chai ₹20]  ->  [0, 0, 1]

    This works for overlapping statements because the index is per
    *identity group*, not per file position: in the overlap, the same
    transactions appear in the same order, so each copy gets the same index
    both times and therefore the same fingerprint. The edge case is a
    statement period that starts *between* two identical rows on the same
    day; see ADR 006.

    Returns a list parallel to `rows` (index i belongs to rows[i]) rather
    than new row objects, so `NormalizedRow` does not need a mutable field.
    """
    seen: dict[tuple[object, ...], int] = {}
    indexes: list[int] = []
    for row in rows:
        key = _identity(row)
        index = seen.get(key, 0)
        seen[key] = index + 1
        indexes.append(index)
    return indexes


def compute_fingerprint(
    account_id: uuid.UUID | str,
    row: NormalizedRow,
    occurrence_index: int,
) -> str:
    """Hash one row's identifying fields into a hex sha256 string.

    `account_id` is part of the hash because the same transaction can
    legitimately exist in two of the user's accounts (a transfer between
    them shows up in both statements), and those are two real rows.

    A missing balance becomes an empty field rather than the text "None",
    so the value is never confused with a narration containing that word.

    sha256 is used as a *content identifier*, not as password protection
    (passwords use argon2id, see `app/core/security.py`): we need a short,
    stable, collision-free-in-practice string for a row's contents.
    """
    parts = (
        str(account_id),
        row.raw.txn_date.isoformat(),
        str(row.raw.amount_paise),
        row.raw.direction.value,
        row.normalized_description,
        "" if row.raw.balance_after_paise is None else str(row.raw.balance_after_paise),
        str(occurrence_index),
    )
    joined = _SEPARATOR.join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def fingerprints_for(account_id: uuid.UUID | str, rows: Sequence[NormalizedRow]) -> list[str]:
    """Fingerprint a whole statement: occurrence indexes, then hashes.

    This is what the import service (Step 7) calls. It exists so that no
    caller can forget to assign the occurrence indexes first, which would
    silently drop the duplicate-looking rows described above.
    """
    indexes = assign_occurrence_indexes(rows)
    return [
        compute_fingerprint(account_id, row, index)
        for row, index in zip(rows, indexes, strict=True)
    ]
