"""Categorize an upload's transactions (SPEC §8, Phase 2 Step 6, ADR 016).

The decisions themselves are made by the pure `app/categorizer/` package.
This module is the part that touches the database:

    1. load the data the chain needs - the merchant dictionary and the ids
       of the system categories (plus, from Step 7, the user's rules)
    2. turn each stored row into `TxnFacts`, run the chain
    3. write the decision back: category_id, category_source, confidence,
       and merchant_id when the merchant key is in the dictionary

It runs inside `process_upload` (app/services/imports.py), in the SAME
database transaction as the insert, so the rows appear already categorized
and a failure rolls both back together (Phase 2 plan).

Only rows whose `category_source` is still `none` are touched, so a
category the user chose by hand (`user_manual`) is never overwritten - the
chain's own guard says the same, belt and braces.
"""

import uuid
from collections import Counter
from dataclasses import dataclass

import structlog
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.categorizer.base import CategorySource, Decision, TxnFacts, categorize
from app.categorizer.handlers import build_chain
from app.models import Category, Merchant, StatementUpload, Transaction
from app.parsers.narration import counterparty_name, dictionary_key

log = structlog.get_logger()


def categorize_upload(db: Session, upload: StatementUpload) -> int:
    """Categorize the not-yet-categorized rows one upload inserted.

    Returns how many rows got a category. Nothing is committed here: the
    caller (`process_upload`) commits the import as a whole.

    Rows that the upload SKIPPED as duplicates belong to the earlier upload
    that inserted them, so they are not looked at again.
    """
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

    merchants = {merchant.normalized_key: merchant for merchant in db.scalars(select(Merchant))}
    usable = _usable_categories(db, upload.user_id)
    chain = build_chain(
        # The user's rules arrive with their table in Step 7.
        rules=(),
        merchant_categories={
            key: merchant.default_category_slug for key, merchant in merchants.items()
        },
    )

    categorized = 0
    unknown_slugs: Counter[str] = Counter()
    refused_ids = 0
    for row in rows:
        # Looked up ONCE per row: the merchant link and the merchant handler
        # both need it. The link is independent of the category: a row a
        # user rule categorizes is still a Zomato row.
        key = dictionary_key(row.merchant_key, merchants.keys())
        row.merchant_id = merchants[key].id if key is not None else None

        decision = categorize(_facts(row, key), chain)
        if decision is None:
            continue
        category_id = _category_id(decision, usable)
        if category_id is None:
            if decision.category_slug is not None:
                unknown_slugs[decision.category_slug] += 1
            else:
                refused_ids += 1
            continue
        row.category_id = category_id
        row.category_source = decision.source
        row.category_confidence = decision.confidence
        categorized += 1

    # ONE line per problem, not one per row: a seed/code mismatch on a
    # two-year statement would otherwise flood the log (review of Step 6).
    # A slug is not personal data, and counts are only counts.
    for slug, count in unknown_slugs.items():
        log.warning("unknown_category_slug", category_slug=slug, rows_count=count)
    if refused_ids:
        log.warning(
            "category_not_usable_by_user",
            user_id=str(upload.user_id),
            upload_id=str(upload.id),
            rows_count=refused_ids,
        )
    return categorized


def _facts(row: Transaction, dictionary_key: str | None) -> TxnFacts:
    """What the chain may see of a stored row (no amount, no date, no raw text).

    The raw narration is read here, in the service, only to pick out the
    counterparty's NAME field; the chain itself never receives it.
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


@dataclass(frozen=True)
class UsableCategories:
    """The categories this user's transactions may be filed under.

    - `system_by_slug`: slug -> id of every SYSTEM category. The built-in
      handlers name categories by slug, and only system rows are read for
      that, so a user category that reuses a slug is never picked up.
    - `ids`: the id of every category the user may use - the system ones
      plus their own. A user rule names its category by id, and that id is
      checked against this set before it is written.
    """

    system_by_slug: dict[str, uuid.UUID]
    ids: frozenset[uuid.UUID]


def _usable_categories(db: Session, user_id: uuid.UUID) -> UsableCategories:
    """System categories plus the user's own: `categories_service`'s rule."""
    stmt = select(Category.id, Category.slug, Category.user_id).where(
        or_(Category.user_id.is_(None), Category.user_id == user_id)
    )
    rows = db.execute(stmt).all()
    return UsableCategories(
        system_by_slug={slug: category_id for category_id, slug, owner in rows if owner is None},
        ids=frozenset(category_id for category_id, _slug, _owner in rows),
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

    The caller logs both cases once per upload.
    """
    if decision.category_id is not None:
        return decision.category_id if decision.category_id in usable.ids else None
    return usable.system_by_slug.get(decision.category_slug or "")
