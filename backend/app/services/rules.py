"""User rules: CRUD, learning one from a correction, and applying them (ADR 017).

Three groups of functions:

- **CRUD** for `/rules` - list, create, update, delete. Every query is
  scoped by `user_id` (rule 2), and a rule's category must be one the user
  may use (a system category or their own), checked through
  `categories_service.get_category` exactly as a manual category is.
- **`learn_rule`** - `PATCH /transactions/{id}` with `apply_to_similar=true`
  turns the corrected row into a rule (SPEC §8.1). What it matches on is
  decided by the pure `learned_rule_key`.
- **`commit_and_apply`** - saves ANY change to a user's rules and re-runs
  the categorizer over their past rows. Normally that is the
  `recategorize_user` job; if Redis cannot be reached it runs right here in
  the request instead (decided with Dhruv, ADR 017), so the user never sees
  a rule that is saved but not applied.

How the two paths keep "saved" and "applied" together is explained in
`commit_and_apply`.
"""

import uuid

import structlog
from redis.exceptions import RedisError
from rq import Queue
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.categorizer.learning import learned_rule_key
from app.core.errors import ConflictError, NotFoundError, NothingToMatchError
from app.db.errors import violates_constraint
from app.models import CategoryRule, Transaction
from app.parsers.narration import dictionary_key
from app.schemas.rule import RuleCreate, RuleUpdate
from app.services import categories as categories_service
from app.services import categorization as categorization_service
from app.workers.queue import enqueue_recategorization

log = structlog.get_logger()

RULE_NOT_FOUND_MESSAGE = "Rule not found."
DUPLICATE_RULE_MESSAGE = "You already have a rule for this; change that one instead."
# Name given by our naming convention (app/db/base.py), see migration 0012.
RULES_UNIQUE_CONSTRAINT = "uq_category_rules_user_id_match_type_pattern"


# --- CRUD -----------------------------------------------------------------


def list_rules(db: Session, user_id: uuid.UUID) -> list[CategoryRule]:
    """All of the user's rules, in the order the categorizer tries them.

    The very query the categorizer loads them with (`rules_in_order`), so
    "first in the list" and "wins" mean the same thing. No pagination: a
    user has tens of rules, not thousands.
    """
    return list(db.scalars(categorization_service.rules_in_order(user_id)))


def get_rule(db: Session, user_id: uuid.UUID, rule_id: uuid.UUID) -> CategoryRule:
    """One of the user's rules, or `NotFoundError` (404) - also for another user's."""
    stmt = select(CategoryRule).where(CategoryRule.id == rule_id, CategoryRule.user_id == user_id)
    rule = db.scalar(stmt)
    if rule is None:
        raise NotFoundError(RULE_NOT_FOUND_MESSAGE)
    return rule


def create_rule(db: Session, queue: Queue, user_id: uuid.UUID, data: RuleCreate) -> CategoryRule:
    """Add a rule and apply it to the past. 404 for a category the user may
    not use, 409 if they already have a rule for the same match.

    Like `create_account`: insert and let the unique constraint catch the
    duplicate, rather than check first and race a parallel request.
    """
    categories_service.get_category(db, user_id, data.category_id)
    # The pattern was normalized by RuleCreate; user_id comes from the
    # token, never from the body.
    rule = CategoryRule(user_id=user_id, **data.model_dump())
    db.add(rule)
    _flush_or_conflict(db)
    commit_and_apply(db, queue, user_id)

    log.info(
        "rule_created",
        user_id=str(user_id),
        rule_id=str(rule.id),
        match_type=rule.match_type.value,  # never the pattern: it may be a VPA
    )
    return rule


def update_rule(
    db: Session, queue: Queue, user_id: uuid.UUID, rule_id: uuid.UUID, data: RuleUpdate
) -> CategoryRule:
    """Change a rule's category and/or priority, and re-apply the rules.

    A priority change alone also re-applies them: it can change which of
    two matching rules wins. A body that changes NOTHING - `{}`, or the
    values the rule already has (a form saved twice) - is answered without
    a commit or a re-run, which would otherwise lock every one of the
    user's rows for nothing (review of Step 7).
    """
    rule = get_rule(db, user_id, rule_id)
    changes = {
        name: value
        for name, value in data.model_dump(exclude_unset=True).items()
        if getattr(rule, name) != value
    }
    if not changes:
        return rule
    if "category_id" in changes:
        categories_service.get_category(db, user_id, changes["category_id"])
    for name, value in changes.items():
        setattr(rule, name, value)
    commit_and_apply(db, queue, user_id)

    log.info("rule_updated", user_id=str(user_id), rule_id=str(rule_id), fields=sorted(changes))
    return rule


def delete_rule(db: Session, queue: Queue, user_id: uuid.UUID, rule_id: uuid.UUID) -> None:
    """Delete a rule. The rows it had categorized are re-run, so they fall
    back to whatever the remaining rules and the built-in chain say."""
    rule = get_rule(db, user_id, rule_id)
    db.delete(rule)
    commit_and_apply(db, queue, user_id)

    log.info("rule_deleted", user_id=str(user_id), rule_id=str(rule_id))


# --- Learning from a correction (SPEC §8.1) -------------------------------


def learn_rule(db: Session, user_id: uuid.UUID, transaction: Transaction) -> uuid.UUID:
    """Write (or update) the rule that files rows like this one in its category.

    Called by `update_transaction` after it set the row's category by hand,
    BEFORE its commit, so the category and the rule are saved together - or
    neither is, when this raises `NothingToMatchError`.

    If the user already has a rule for the same match, its category is
    replaced: the latest correction wins (decided with Dhruv, ADR 017).
    That is one statement, `INSERT ... ON CONFLICT ... DO UPDATE` (an
    "upsert"): Postgres inserts the row, or, if the unique constraint says
    it exists, updates that one instead - atomically, so two corrections at
    the same moment cannot both insert. Returns the rule's id.
    """
    key = dictionary_key(transaction.merchant_key, categorization_service.merchant_keys(db))
    match = learned_rule_key(categorization_service.facts_of(transaction, key))
    if match is None:
        raise NothingToMatchError()
    match_type, pattern = match

    stmt = insert(CategoryRule).values(
        id=uuid.uuid4(),
        user_id=user_id,
        match_type=match_type,
        pattern=pattern,
        category_id=transaction.category_id,
        created_from_transaction_id=transaction.id,
    )
    stmt = stmt.on_conflict_do_update(
        constraint=RULES_UNIQUE_CONSTRAINT,
        # `excluded` is Postgres' name for the row that failed to insert.
        # `updated_at` by hand: the ORM's onupdate does not run for Core SQL.
        set_={
            "category_id": stmt.excluded.category_id,
            "created_from_transaction_id": stmt.excluded.created_from_transaction_id,
            "updated_at": func.now(),
        },
    ).returning(CategoryRule.id)
    rule_id = db.execute(stmt).scalar_one()

    log.info(
        "rule_learned",
        user_id=str(user_id),
        rule_id=str(rule_id),
        transaction_id=str(transaction.id),
        match_type=match_type.value,
    )
    return rule_id


# --- Applying the rules to the past ---------------------------------------


def commit_and_apply(db: Session, queue: Queue, user_id: uuid.UUID) -> None:
    """Commit a change to the user's rules, and re-categorize their past rows.

    Call it with the rule change made but NOT committed. Two paths:

    - **Redis is up (the normal case):** commit, then enqueue the
      `recategorize_user` job (SPEC §11): a user with years of statements
      should not wait for it. Commit FIRST because the job runs in another
      process and can only see committed rows; enqueued before the commit,
      a fast worker could re-categorize with the old rules.
    - **Redis is down:** the re-run happens HERE, in the same database
      transaction as the rule change, and `recategorize_user` commits both
      together. If the re-run fails, both roll back: the client gets an
      error with nothing saved, and simply tries again. (Committing the
      rule first would turn a failed re-run into "500, but the rule was
      saved", and the retry into a 409 - review of Step 7.)

    Redis is asked with a PING before anything is committed, because which
    path to take must be known BEFORE the commit. If it answers but then
    fails to take the job - it went down in that instant - the rule is
    already committed, and the re-run is done here after all, as its own
    transaction. That gap is a few milliseconds wide.
    """
    if not _queue_is_reachable(queue):
        log.warning("recategorize_queue_down_running_inline", user_id=str(user_id))
        categorization_service.recategorize_user(db, user_id)  # commits both
        return

    db.commit()
    try:
        enqueue_recategorization(queue, user_id)
    except RedisError:
        log.warning("recategorize_enqueue_failed_running_inline", user_id=str(user_id))
        categorization_service.recategorize_user(db, user_id)


def _queue_is_reachable(queue: Queue) -> bool:
    """Does the queue's Redis answer? (2 s timeout on the API's client.)"""
    try:
        queue.connection.ping()
    except RedisError:
        return False
    return True


def _flush_or_conflict(db: Session) -> None:
    """Send the INSERT now; turn a duplicate rule into a 409, anything else a 500.

    A flush, not a commit: `commit_and_apply` decides when to commit. The
    unique constraint is checked by the INSERT itself, so the 409 still
    comes from the database, before anything else is done.
    """
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        if violates_constraint(exc, RULES_UNIQUE_CONSTRAINT):
            raise ConflictError(DUPLICATE_RULE_MESSAGE) from None
        raise
