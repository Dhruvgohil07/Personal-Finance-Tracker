"""Transactions business logic: the filtered list, and manual categorization.

Two jobs, and both of them are about the same table:

- `list_transactions` - the endpoint the whole app is read through. It
  builds ONE SELECT out of the filters the client sent and returns a page
  of rows plus the cursor for the next page (SPEC §10).
- `update_transaction` - the user correcting a row by hand: its category
  and their own note (SPEC §8.1 will later learn a rule from this; Phase 2).

Two things to understand before reading the code.

**Rule 2 is enforced in three places here, not one.** Every query starts
with `Transaction.user_id == user_id`, and on top of that the two filters
that take an id of their own - `account_id` and `category_id` - are looked
up through their services first, so another user's id answers 404 instead
of quietly returning an empty page. That matters for more than tidiness:
an empty list is also a valid answer ("you have no transactions there"),
so without the lookup the client could not tell "nothing matched" from
"that was not your account".

**The filters are applied to one query object, not combined by hand.**
SQLAlchemy lets `.where()` be called again and again, each call ANDing one
more condition, so each filter is three readable lines and the values
always travel as BOUND PARAMETERS - never formatted into the SQL
(CLAUDE.md §7.3, rule: parameterized queries only).
"""

import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import Select, select, tuple_
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models import CategorySource, Transaction
from app.schemas.transaction import TransactionFilters, TransactionUpdate
from app.services import accounts as accounts_service
from app.services import categories as categories_service
from app.utils.cursor import encode_cursor

log = structlog.get_logger()

TRANSACTION_NOT_FOUND_MESSAGE = "Transaction not found."

# Characters that mean something special inside a SQL LIKE/ILIKE pattern.
# A user searching for "50% off" means a literal "%", so each of these is
# prefixed with a backslash, and the query declares that backslash as its
# ESCAPE character. (This is about the PATTERN's own syntax, not about SQL
# injection - the pattern is a bound parameter either way.)
_LIKE_SPECIAL_CHARS = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})
_LIKE_ESCAPE_CHAR = "\\"


@dataclass(frozen=True)
class TransactionList:
    """One page of transactions and the cursor that continues it.

    A plain dataclass, not a Pydantic model: services return ORM rows and
    the route turns them into the response model (rule 11). This only ties
    the rows and the cursor together so the route does not have to compute
    the cursor itself.
    """

    rows: list[Transaction]
    # None means "this was the last page".
    next_cursor: str | None


def list_transactions(
    db: Session, user_id: uuid.UUID, filters: TransactionFilters
) -> TransactionList:
    """The user's transactions, newest first, filtered and paginated.

    Ordered by `(txn_date DESC, id DESC)`, which is both what a statement
    view wants and the key the cursor uses. `id` is not decoration: two
    transactions on the same day must still have ONE defined order, or a
    page boundary could repeat a row and skip another.

    Reads one row more than the client asked for (`limit + 1`). If that
    extra row exists there is a next page, and the cursor is built from the
    LAST row of the page - which is why the extra row is dropped before
    returning. Asking for one more row is cheaper and more reliable than a
    second `COUNT(*)` query, which would also need the same filters and
    could disagree with the page it describes.
    """
    # 404 for an id that is not the caller's, before any rows are read.
    if filters.account_id is not None:
        accounts_service.get_account(db, user_id, filters.account_id)
    if filters.category_id is not None:
        categories_service.get_category(db, user_id, filters.category_id)

    stmt = _filtered_query(user_id, filters).order_by(
        Transaction.txn_date.desc(), Transaction.id.desc()
    )
    rows = list(db.scalars(stmt.limit(filters.limit + 1)))

    if len(rows) <= filters.limit:
        return TransactionList(rows=rows, next_cursor=None)

    page = rows[: filters.limit]
    last = page[-1]
    return TransactionList(rows=page, next_cursor=encode_cursor(last.txn_date, last.id))


def get_transaction(db: Session, user_id: uuid.UUID, transaction_id: uuid.UUID) -> Transaction:
    """One of the user's transactions, or `NotFoundError` (404).

    Same IDOR rule as elsewhere: another user's transaction and a
    transaction that does not exist give the identical 404, so the id space
    cannot be probed.
    """
    stmt = select(Transaction).where(
        Transaction.id == transaction_id, Transaction.user_id == user_id
    )
    transaction = db.scalar(stmt)
    if transaction is None:
        raise NotFoundError(TRANSACTION_NOT_FOUND_MESSAGE)
    return transaction


def update_transaction(
    db: Session, user_id: uuid.UUID, transaction_id: uuid.UUID, data: TransactionUpdate
) -> Transaction:
    """Set the category and/or the note on one of the user's transactions.

    PATCH semantics with a twist that `AccountUpdate` does not need: here
    an explicit null is a real instruction ("remove the category", "remove
    the note"), so `exclude_unset=True` is what separates "not sent" from
    "set to null". A field that was not sent is not touched.

    Setting a category by hand also records WHO decided it
    (`category_source = user_manual`). That flag is what stops the Phase 2
    categorizer from overwriting the user's own correction, and it is what
    SPEC §8.1 later learns a rule from. Clearing the category puts the row
    back to `none`, so the categorizer may look at it again.

    `category_confidence` is set to NULL in both cases: it describes how
    sure a GUESS was, and a person choosing a category is not a guess.
    """
    transaction = get_transaction(db, user_id, transaction_id)
    changes = data.model_dump(exclude_unset=True)

    if "category_id" in changes:
        _set_category(db, user_id, transaction, changes["category_id"])
    if "notes" in changes:
        # An empty or whitespace-only note becomes NULL, so "no note" has
        # exactly one representation in the database.
        transaction.notes = changes["notes"] or None

    db.commit()
    # Field NAMES only - never the note's text or the category id's meaning
    # for this user (CLAUDE.md rule 3).
    log.info(
        "transaction_updated",
        user_id=str(user_id),
        transaction_id=str(transaction_id),
        fields=sorted(changes),
    )
    return transaction


# --- Helpers --------------------------------------------------------------


def _set_category(
    db: Session, user_id: uuid.UUID, transaction: Transaction, category_id: uuid.UUID | None
) -> None:
    """Apply a category change, with its bookkeeping columns."""
    if category_id is None:
        transaction.category_id = None
        transaction.category_source = CategorySource.NONE
        transaction.category_confidence = None
        return

    # 404 unless it is a system category or one of the user's own.
    category = categories_service.get_category(db, user_id, category_id)
    transaction.category_id = category.id
    transaction.category_source = CategorySource.USER_MANUAL
    transaction.category_confidence = None


def _filtered_query(user_id: uuid.UUID, filters: TransactionFilters) -> Select[tuple[Transaction]]:
    """Build the SELECT for `list_transactions` (without the ORDER BY/LIMIT).

    Each filter is optional and simply ANDs one more condition. They are
    written in the order SPEC §10 lists them, so the endpoint's
    documentation and this function can be read side by side.
    """
    stmt = select(Transaction).where(Transaction.user_id == user_id)

    if filters.date_from is not None:
        stmt = stmt.where(Transaction.txn_date >= filters.date_from)
    if filters.date_to is not None:
        # Inclusive: "to=2026-08-31" includes the 31st. `txn_date` is a
        # DATE (no time of day), so there is no "23:59" edge case here.
        stmt = stmt.where(Transaction.txn_date <= filters.date_to)
    if filters.account_id is not None:
        stmt = stmt.where(Transaction.account_id == filters.account_id)
    if filters.category_id is not None:
        stmt = stmt.where(Transaction.category_id == filters.category_id)
    if filters.direction is not None:
        stmt = stmt.where(Transaction.direction == filters.direction)
    if filters.q is not None:
        stmt = stmt.where(
            Transaction.normalized_description.ilike(
                _search_pattern(filters.q), escape=_LIKE_ESCAPE_CHAR
            )
        )

    # Amounts are compared in paise, always positive, so these bounds mean
    # "size of the transaction" regardless of direction (ADR 001).
    min_paise = filters.min_amount_paise
    if min_paise is not None:
        stmt = stmt.where(Transaction.amount_paise >= min_paise)
    max_paise = filters.max_amount_paise
    if max_paise is not None:
        stmt = stmt.where(Transaction.amount_paise <= max_paise)

    cursor_key = filters.cursor_key()
    if cursor_key is not None:
        # The keyset condition. Rows are ordered by (txn_date DESC, id
        # DESC), so "after the cursor" means "smaller than the cursor" -
        # and SQL can compare the whole pair at once:
        #
        #   WHERE (txn_date, id) < ('2026-08-04', '...uuid...')
        #
        # which is exactly how the ix_transactions_user_txn_date_id index
        # is sorted, so Postgres jumps to that position in the index
        # instead of scanning and sorting. Written as the equivalent
        # `txn_date < d OR (txn_date = d AND id < i)` it would mean the
        # same thing but read worse and optimize less reliably.
        stmt = stmt.where(tuple_(Transaction.txn_date, Transaction.id) < cursor_key)

    return stmt


def _search_pattern(text: str) -> str:
    """Turn the user's `q=` into a "contains" ILIKE pattern.

    `%zomato%` matches the text anywhere in the narration, case
    insensitively (ILIKE). A leading wildcard is normally unindexable, but
    `normalized_description` has a trigram GIN index
    (ix_transactions_normalized_description_trgm), which indexes every
    3-character slice of the text - so a search of 3 characters or more can
    still use it. Shorter searches fall back to a scan of the user's rows,
    which is fine at this size.

    Note the search runs against the NORMALIZED narration (uppercased,
    collapsed whitespace, long digit runs removed - SPEC §6.5), so
    searching for an account number deliberately finds nothing, while
    "zomato" finds "UPI/ZOMATO ONLINE/..." whatever case it was typed in.
    """
    return f"%{text.translate(_LIKE_SPECIAL_CHARS)}%"
