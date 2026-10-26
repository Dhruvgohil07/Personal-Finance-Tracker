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

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.categorizer.base import CategorySource, Decision, TxnFacts, categorize
from app.categorizer.handlers import build_chain
from app.models import Category, Merchant, StatementUpload, Transaction
from app.parsers.narration import dictionary_key

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
    category_ids = _system_category_ids(db)
    chain = build_chain(
        # The user's rules arrive with their table in Step 7.
        rules=(),
        merchant_categories={
            key: merchant.default_category_slug for key, merchant in merchants.items()
        },
    )

    categorized = 0
    for row in rows:
        # The merchant link is independent of the category: a row a user
        # rule categorizes is still a Zomato row.
        key = dictionary_key(row.merchant_key, merchants.keys())
        row.merchant_id = merchants[key].id if key is not None else None

        decision = categorize(_facts(row), chain)
        if decision is None:
            continue
        category_id = _category_id(decision, category_ids)
        if category_id is None:
            continue
        row.category_id = category_id
        row.category_source = decision.source
        row.category_confidence = decision.confidence
        categorized += 1
    return categorized


def _facts(row: Transaction) -> TxnFacts:
    """What the chain may see of a stored row (no amount, no date, no raw text)."""
    return TxnFacts(
        direction=row.direction,
        channel=row.channel,
        normalized_description=row.normalized_description,
        merchant_key=row.merchant_key,
        counterparty_vpa=row.counterparty_vpa,
        category_source=row.category_source,
    )


def _system_category_ids(db: Session) -> dict[str, uuid.UUID]:
    """slug -> id of every SYSTEM category (user_id IS NULL).

    The built-in handlers name system categories by slug. Only system rows
    are read, so a user category that happens to reuse a slug can never be
    picked up for another user's transaction.
    """
    stmt = select(Category.slug, Category.id).where(Category.user_id.is_(None))
    return {slug: category_id for slug, category_id in db.execute(stmt)}


def _category_id(decision: Decision, category_ids: dict[str, uuid.UUID]) -> uuid.UUID | None:
    """The category id a decision names, or None if its slug is not seeded.

    A slug that is not in the database means the seed and the code disagree
    (a merchant pointing at a renamed category, say). The row is left
    uncategorized rather than failing the whole import, and the slug is
    logged so the mismatch is found - a slug is not personal data.
    """
    if decision.category_id is not None:
        return decision.category_id
    category_id = category_ids.get(decision.category_slug or "")
    if category_id is None:
        log.warning("unknown_category_slug", category_slug=decision.category_slug)
    return category_id
