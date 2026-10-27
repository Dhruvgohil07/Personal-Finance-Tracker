"""Categorize transactions with the chain (SPEC §8; ADR 016, ADR 017, ADR 018).

The decisions themselves are made by the pure `app/categorizer/` package.
This module is the part that touches the database:

    1. load the data the chain needs - the merchant dictionary, the ids of
       the categories the user may use, and the user's own rules
    2. turn each stored row into `TxnFacts`, run the chain
    3. write the decision back: category_id, category_source, confidence,
       is_self_transfer, and merchant_id when the key is in the dictionary
    4. find self-transfer pairs across all the user's accounts and file
       both rows under Self Transfer (`_detect_self_transfers`, Step 8)

Two entry points, for the two moments categorization happens:

- `categorize_upload` - right after an import, inside `process_upload`
  (app/services/imports.py) and in the SAME database transaction as the
  insert, so the rows appear already categorized (Phase 2 plan). Only the
  upload's `none` rows are looked at.
- `recategorize_user` - after one of the user's rules changed (Step 7).
  Re-runs the chain over EVERY row of the user except `user_manual` ones,
  so a non-manual row always shows what the current rules say: a new rule
  takes effect on the past, and a deleted rule stops having one (decided
  with Dhruv, ADR 017). Run by the `recategorize_user` job.

A category the user chose by hand (`user_manual`) is never touched by
either: both queries leave those rows out, and the chain's own guard says
the same, belt and braces.

**One user's categorization runs one at a time.** Both entry points first
take a per-user lock (`lock_categorization`), and only then read the rules.
Without it, two runs could overlap - an import that read the rules a
moment before a rule changed, or two rule changes in quick succession -
and the one that commits LAST would win with rules that are already out
of date. With it, whichever run goes second reads the rules the first one
left, so the last word is always the newest rules.
"""

import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

import structlog
from redis.exceptions import RedisError
from rq import Queue
from sqlalchemy import ColumnElement, Select, exists, func, or_, select
from sqlalchemy.orm import Session

from app.categorizer.base import CategorySource, Decision, Handler, TxnFacts, categorize
from app.categorizer.handlers import Rule, build_chain
from app.categorizer.self_transfer import TransferCandidate, match_self_transfers
from app.models import (
    Category,
    CategoryRule,
    Merchant,
    StatementUpload,
    Transaction,
)
from app.parsers.narration import counterparty_name, dictionary_key
from app.workers.queue import enqueue_recategorization

log = structlog.get_logger()

# Self-transfer detection (Step 8, ADR 018). The pair's category, and how
# sure a matched pair is: an equal amount across two of your own accounts
# within two days is strong evidence, but still a guess, so below a rule's
# 1.00 and above the P2P guess's 0.70.
SELF_TRANSFER = "self-transfer"
SELF_TRANSFER_CONFIDENCE = Decimal("0.80")
# Which rows detection may pair. Never a manual row (the user decided), and
# never one a user rule decided: a rule wins over detection, because the
# user said so (decided with Dhruv, ADR 018).
_PAIRABLE = Transaction.category_source.not_in(
    (CategorySource.USER_MANUAL, CategorySource.USER_RULE)
)


def categorize_upload(db: Session, upload: StatementUpload) -> int:
    """Categorize the not-yet-categorized rows one upload inserted.

    Returns how many rows got a category. Nothing is committed here: the
    caller (`process_upload`) commits the import as a whole, which also
    releases the per-user lock.

    Rows that the upload SKIPPED as duplicates belong to the earlier upload
    that inserted them, so they are not looked at again.
    """
    lock_categorization(db, upload.user_id)
    rows = db.scalars(
        select(Transaction).where(
            # user_id as well as upload_id: every query is scoped by user
            # (CLAUDE.md rule 2), even where the upload already implies it.
            Transaction.user_id == upload.user_id,
            Transaction.upload_id == upload.id,
            Transaction.category_source == CategorySource.NONE,
        )
    ).all()
    if not rows:
        return 0

    context = _load_context(db, upload.user_id)
    outcome = _categorize_rows(rows, context)
    # After the chain, over ALL the user's accounts: a new row may be the
    # other half of a transfer imported long ago (Step 8, ADR 018).
    _detect_self_transfers(db, upload.user_id, context, outcome)
    outcome.log_problems(user_id=upload.user_id, upload_id=upload.id)
    return outcome.categorized


def recategorize_user(db: Session, user_id: uuid.UUID) -> int:
    """Re-run the chain over all of the user's rows except manual ones, and commit.

    Returns how many rows changed. Safe to run any number of times
    (SPEC §11): with the same rules it changes nothing the second time, and
    SQLAlchemy sends no UPDATE for a row whose values came out the same.

    The rows are read `FOR UPDATE` (each one locked until the commit). That
    closes a race with `PATCH /transactions/{id}`: if the user re-files a
    row by hand while this runs, either the PATCH waits for this commit and
    then wins, or this waits for the PATCH - and Postgres then re-checks
    the WHERE clause, finds the row is `user_manual` now, and skips it.
    Without the lock, this could overwrite a choice the user had just made.
    """
    lock_categorization(db, user_id)
    rows = db.scalars(
        select(Transaction)
        .where(
            Transaction.user_id == user_id,
            Transaction.category_source != CategorySource.USER_MANUAL,
        )
        .with_for_update()
    ).all()

    context = _load_context(db, user_id)
    before = [_category_state(row) for row in rows]
    outcome = _categorize_rows(rows, context)
    # The chain has just reset every non-manual row, detected pairs
    # included; detection puts the pairs back. Without it, every rule
    # change would undo the self transfers (ADR 017, ADR 018).
    _detect_self_transfers(db, user_id, context, outcome)
    changed = sum(1 for row, old in zip(rows, before, strict=True) if _category_state(row) != old)
    db.commit()

    outcome.log_problems(user_id=user_id)
    # Counts only (CLAUDE.md rule 3).
    log.info(
        "transactions_recategorized",
        user_id=str(user_id),
        rows_count=len(rows),
        rows_changed=changed,
        rows_categorized=outcome.categorized,
    )
    return changed


def commit_and_recategorize(db: Session, queue: Queue, user_id: uuid.UUID) -> None:
    """Commit a change that affects categorization, and re-run the user's rows.

    For every change after which "the chain, then detection" can give a
    different answer: a rule created, changed or deleted (app/services/
    rules.py), and a self-transfer pair broken by hand or by a delete
    (Step 8). Call it with the change made but NOT committed. Two paths:

    - **Redis is up (the normal case):** commit, then enqueue the
      `recategorize_user` job (SPEC §11): a user with years of statements
      should not wait for it. Commit FIRST because the job runs in another
      process and can only see committed rows; enqueued before the commit,
      a fast worker could re-categorize with the old state.
    - **Redis is down:** the re-run happens HERE, in the same database
      transaction as the change, and `recategorize_user` commits both
      together. If the re-run fails, both roll back: the client gets an
      error with nothing saved, and simply tries again. (Committing first
      would turn a failed re-run into "500, but the change was saved" -
      review of Step 7.)

    Redis is asked with a PING before anything is committed, because which
    path to take must be known BEFORE the commit. If it answers but then
    fails to take the job - it went down in that instant - the change is
    already committed, and the re-run is done here after all, as its own
    transaction. That gap is a few milliseconds wide.

    Lived in app/services/rules.py until the review of Step 8, when it
    stopped being only about rules.
    """
    if not _queue_is_reachable(queue):
        log.warning("recategorize_queue_down_running_inline", user_id=str(user_id))
        recategorize_user(db, user_id)  # commits both
        return

    db.commit()
    recategorize_after_commit(db, queue, user_id)


def recategorize_after_commit(db: Session, queue: Queue, user_id: uuid.UUID) -> None:
    """Re-run the user's rows after a change that is ALREADY committed.

    Enqueue the job, or run it here as its own transaction if Redis will
    not take it. Used directly by the deletes (an upload, an account): a
    delete cascades into rows a running import or re-run may hold, so it
    must not take the categorization lock inside its own transaction (see
    `lock_categorization`). The price: if Redis is down AND this inline
    re-run fails, the delete stands and the re-run waits for the next
    import or rule change - logged by the caller's error handler.
    """
    try:
        enqueue_recategorization(queue, user_id)
    except RedisError:
        log.warning("recategorize_enqueue_failed_running_inline", user_id=str(user_id))
        recategorize_user(db, user_id)


def has_detected_self_transfer(db: Session, user_id: uuid.UUID, where: ColumnElement[bool]) -> bool:
    """Does any of the user's rows matching `where` belong to a detected pair?

    Asked by a delete BEFORE it deletes: if it removes half of a pair, the
    other half needs a re-run, or it would stay out of the totals with no
    partner (review of Step 8). The same test as `is_detected_self_transfer`,
    in SQL.
    """
    stmt = select(
        exists().where(
            Transaction.user_id == user_id,
            Transaction.category_source == CategorySource.HEURISTIC,
            Transaction.is_self_transfer,
            where,
        )
    )
    return bool(db.scalar(stmt))


def _queue_is_reachable(queue: Queue) -> bool:
    """Does the queue's Redis answer? (2 s timeout on the API's client.)"""
    try:
        queue.connection.ping()
    except RedisError:
        return False
    return True


# --- The shared core ------------------------------------------------------


@dataclass(frozen=True)
class UsableCategories:
    """The categories this user's transactions may be filed under.

    - `system_by_slug`: slug -> id of every SYSTEM category. The built-in
      handlers name categories by slug, and only system rows are read for
      that, so a user category that reuses a slug is never picked up.
    - `ids`: the id of every category the user may use - the system ones
      plus their own. A user rule names its category by id, and that id is
      checked against this set before it is written.
    - `self_transfer_ids`: those whose kind `marks_self_transfer` (Self
      Transfer). Filing a row under one also marks it `is_self_transfer`,
      by the same rule a manual choice uses (`CategoryKind`), so the flag
      the insights read never disagrees with the category.
    """

    system_by_slug: dict[str, uuid.UUID]
    ids: frozenset[uuid.UUID]
    self_transfer_ids: frozenset[uuid.UUID]


@dataclass(frozen=True)
class _Context:
    """Everything one run needs, loaded once rather than once per row."""

    merchants: dict[str, Merchant]  # dictionary key -> merchant row
    usable: UsableCategories
    chain: Sequence[Handler]


@dataclass
class _Outcome:
    """What one run did, counted rather than logged row by row."""

    categorized: int = 0
    unknown_slugs: Counter[str] = field(default_factory=Counter)
    refused_ids: int = 0

    def log_problems(self, *, user_id: uuid.UUID, upload_id: uuid.UUID | None = None) -> None:
        """ONE line per problem, not one per row (review of Step 6).

        A seed/code mismatch on a two-year statement would otherwise flood
        the log. A slug is not personal data, and counts are only counts.
        """
        for slug, count in self.unknown_slugs.items():
            log.warning("unknown_category_slug", category_slug=slug, rows_count=count)
        if self.refused_ids:
            ids = {"user_id": str(user_id)}
            if upload_id is not None:
                ids["upload_id"] = str(upload_id)
            log.warning("category_not_usable_by_user", **ids, rows_count=self.refused_ids)


def lock_categorization(db: Session, user_id: uuid.UUID) -> None:
    """Take this user's categorization lock until the transaction ends.

    `pg_advisory_xact_lock(key)` is a Postgres lock on a NUMBER instead of
    a row: any two sessions asking for the same number take turns, and the
    lock is released by the commit or rollback (`xact`), so it cannot be
    forgotten. The number is the first 8 bytes of the user's UUID read as a
    signed 64-bit integer (the type Postgres takes). Two users sharing those
    bytes would only wait for each other, never get a wrong answer; nothing
    else in the app uses advisory locks, so no other lock can collide.

    **Lock order: this lock FIRST, transaction rows after.** A run holds it
    and then locks rows (`FOR UPDATE`). A request that changes a row and
    may then re-run in its own transaction (Redis down) must therefore take
    this lock BEFORE it touches the row. The other way round, the request
    would hold the row and wait for this lock while a running job held this
    lock and waited for the row: a deadlock (review of Step 8). Public for
    exactly those callers.
    """
    key = int.from_bytes(user_id.bytes[:8], "big", signed=True)
    db.execute(select(func.pg_advisory_xact_lock(key)))


def _load_context(db: Session, user_id: uuid.UUID) -> _Context:
    merchants = {merchant.normalized_key: merchant for merchant in db.scalars(select(Merchant))}
    chain = build_chain(
        rules=_load_rules(db, user_id),
        merchant_categories={
            key: merchant.default_category_slug for key, merchant in merchants.items()
        },
    )
    return _Context(merchants=merchants, usable=_usable_categories(db, user_id), chain=chain)


def rules_in_order(user_id: uuid.UUID) -> Select[tuple[CategoryRule]]:
    """The user's rules, in the order the chain tries them.

    The ONE definition of that order, shared by `_load_rules` and by
    `GET /rules` (app/services/rules.py), so "first in the list" and
    "wins" can never drift apart (review of Step 7).

    Lowest priority number first; among equal priorities the rule created
    or CHANGED most recently first, so the latest correction wins (ADR 017).
    `updated_at`, not `created_at`: a correction that re-teaches an existing
    rule updates it in place (the upsert in `learn_rule`), and must then
    beat a rule written after the original one. `id` makes the order total
    even for two rules changed in the same transaction.
    """
    return (
        select(CategoryRule)
        .where(CategoryRule.user_id == user_id)
        .order_by(CategoryRule.priority, CategoryRule.updated_at.desc(), CategoryRule.id)
    )


def _load_rules(db: Session, user_id: uuid.UUID) -> list[Rule]:
    """The user's rules as the chain's plain `Rule`s, in `rules_in_order`.

    `UserRuleHandler` sorts by priority with a stable sort, which keeps
    this order within a priority.
    """
    return [
        Rule(rule.match_type, rule.pattern, rule.category_id, rule.priority)
        for rule in db.scalars(rules_in_order(user_id))
    ]


def _categorize_rows(rows: Sequence[Transaction], context: _Context) -> _Outcome:
    """Run the chain on each row and write the result onto it (not committed)."""
    outcome = _Outcome()
    for row in rows:
        # Looked up ONCE per row: the merchant link and the merchant handler
        # both need it. The link is independent of the category: a row a
        # user rule categorizes is still a Zomato row.
        key = dictionary_key(row.merchant_key, context.merchants.keys())
        row.merchant_id = context.merchants[key].id if key is not None else None

        decision = categorize(facts_of(row, key), context.chain)
        category_id = None if decision is None else _category_id(decision, context.usable)
        if decision is not None and category_id is None:
            if decision.category_slug is not None:
                outcome.unknown_slugs[decision.category_slug] += 1
            else:
                outcome.refused_ids += 1

        if decision is None or category_id is None:
            # Nothing decided (or nothing that may be written): the row is
            # "not categorized yet". For a new row that is what it already
            # was; for a re-run it undoes what a deleted rule had decided.
            row.category_id = None
            row.category_source = CategorySource.NONE
            row.category_confidence = None
            row.is_self_transfer = False
            continue
        row.category_id = category_id
        row.category_source = decision.source
        row.category_confidence = decision.confidence
        row.is_self_transfer = category_id in context.usable.self_transfer_ids
        outcome.categorized += 1
    return outcome


def _detect_self_transfers(
    db: Session, user_id: uuid.UUID, context: _Context, outcome: _Outcome
) -> None:
    """Find the user's self-transfer pairs and file both rows under Self Transfer.

    Runs at the end of BOTH categorization passes, under the same per-user
    lock, so a non-manual row always shows "the chain, then detection"
    (ADR 018). Over all of the user's PAIRABLE rows (`_PAIRABLE`), in every
    account, because a pair spans two accounts and often two uploads.

    Two things happen:

    - every row in a pair becomes Self Transfer, `heuristic`, 0.80, with
      `is_self_transfer` set - overriding a built-in guess such as "P2P",
      which is exactly what a payment to your own account looks like;
    - a row that WAS detected before but is no longer in a pair (its
      partner was re-filed by hand, or a new import made it ambiguous) is
      re-decided by the chain, instead of staying a self transfer with no
      partner. In the re-run pass the chain already did that for every
      row; in the import pass this is what does it.

    The rows are read `FOR UPDATE` for the same reason as in
    `recategorize_user`: a manual choice made meanwhile must win.
    """
    rows = db.scalars(
        select(Transaction).where(Transaction.user_id == user_id, _PAIRABLE).with_for_update()
    ).all()
    pairs = match_self_transfers(
        [
            TransferCandidate(row.id, row.account_id, row.direction, row.amount_paise, row.txn_date)
            for row in rows
        ]
    )
    paired = {row_id for pair in pairs for row_id in pair}

    unpaired = [row for row in rows if row.id not in paired and is_detected_self_transfer(row)]
    if unpaired:
        redone = _categorize_rows(unpaired, context)
        outcome.unknown_slugs.update(redone.unknown_slugs)
        outcome.refused_ids += redone.refused_ids

    category_id = context.usable.system_by_slug.get(SELF_TRANSFER)
    if category_id is None:
        # The seed and the code disagree (as for any unknown slug): the
        # pairs stay as the chain left them, counted once in the log.
        if paired:
            outcome.unknown_slugs[SELF_TRANSFER] += len(paired)
        return
    for row in rows:
        if row.id in paired:
            row.category_id = category_id
            row.category_source = CategorySource.HEURISTIC
            row.category_confidence = SELF_TRANSFER_CONFIDENCE
            row.is_self_transfer = True

    # Counts only (CLAUDE.md rule 3).
    log.info(
        "self_transfers_detected",
        user_id=str(user_id),
        pairs_count=len(pairs),
        rows_unpaired=len(unpaired),
    )


def is_detected_self_transfer(row: Transaction) -> bool:
    """Was this row marked by self-transfer detection (not by the chain)?

    Detection is the only thing that writes `heuristic` together with
    `is_self_transfer`: the chain's one heuristic, P2P, files people under
    an expense or income category, and a user rule is `user_rule`.
    """
    return row.category_source is CategorySource.HEURISTIC and row.is_self_transfer


def _category_state(row: Transaction) -> tuple[object, ...]:
    """The columns a run may change, to count the rows it really changed."""
    return (
        row.category_id,
        row.category_source,
        row.category_confidence,
        row.is_self_transfer,
        row.merchant_id,
    )


def facts_of(row: Transaction, dictionary_key: str | None) -> TxnFacts:
    """What the chain may see of a stored row (no amount, no date, no raw text).

    The raw narration is read here, in the service, only to pick out the
    counterparty's NAME field; the chain itself never receives it. Public
    because rule learning (app/services/rules.py) asks the same facts.
    """
    return TxnFacts(
        direction=row.direction,
        channel=row.channel,
        normalized_description=row.normalized_description,
        merchant_key=row.merchant_key,
        counterparty_vpa=row.counterparty_vpa,
        dictionary_key=dictionary_key,
        counterparty_name=counterparty_name(row.raw_description),
        category_source=row.category_source,
    )


def merchant_keys(db: Session) -> list[str]:
    """Every key of the merchant dictionary, for `dictionary_key` lookups."""
    return list(db.scalars(select(Merchant.normalized_key)))


def _usable_categories(db: Session, user_id: uuid.UUID) -> UsableCategories:
    """System categories plus the user's own: `categories_service`'s rule."""
    stmt = select(Category.id, Category.slug, Category.user_id, Category.kind).where(
        or_(Category.user_id.is_(None), Category.user_id == user_id)
    )
    rows = db.execute(stmt).all()
    return UsableCategories(
        system_by_slug={
            slug: category_id for category_id, slug, owner, _kind in rows if owner is None
        },
        ids=frozenset(category_id for category_id, _slug, _owner, _kind in rows),
        self_transfer_ids=frozenset(
            category_id for category_id, _slug, _owner, kind in rows if kind.marks_self_transfer
        ),
    )


def _category_id(decision: Decision, usable: UsableCategories) -> uuid.UUID | None:
    """The category id to write for a decision, or None if it must not be written.

    - A slug that is not seeded means the seed and the code disagree (a
      merchant pointing at a renamed category, say). The row is left
      uncategorized rather than failing the whole import.
    - An id (a user rule's category) is written only if it is a system
      category or the user's own. The insights breakdown joins categories
      WITHOUT a user filter (ADR 010), which is safe only while every stored
      `category_id` passes exactly this check - so it is enforced where the
      id is written, not only where rules are created (review of Step 6).

    The caller counts both cases and logs them once per run.
    """
    if decision.category_id is not None:
        return decision.category_id if decision.category_id in usable.ids else None
    return usable.system_by_slug.get(decision.category_slug or "")
