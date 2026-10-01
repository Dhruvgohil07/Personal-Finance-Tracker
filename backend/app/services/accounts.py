"""Accounts business logic: list, create, get, update, delete.

Rule 2 (every query is scoped by user_id): every function takes the
`user_id` of the logged-in user and puts it in the WHERE clause. An
account id alone is never enough to reach a row.
"""

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.models import Account
from app.schemas.account import AccountCreate, AccountUpdate

log = structlog.get_logger()

ACCOUNT_NOT_FOUND_MESSAGE = "Account not found."
DUPLICATE_ACCOUNT_MESSAGE = "You have already added this account."


def list_accounts(db: Session, user_id: uuid.UUID) -> list[Account]:
    """All of the user's accounts, oldest first (a user has only a handful,
    so no pagination)."""
    stmt = select(Account).where(Account.user_id == user_id).order_by(Account.created_at)
    # scalars() gives Account objects instead of one-column rows.
    return list(db.scalars(stmt))


def create_account(db: Session, user_id: uuid.UUID, data: AccountCreate) -> Account:
    """Add an account. ConflictError (409) if the user already has it.

    Like register_user: insert and let the unique constraint
    (user_id, bank_code, masked_number) catch duplicates, instead of a
    check-then-insert that two parallel requests could both pass.
    """
    # model_dump() turns the validated body into a dict of column values.
    # user_id comes from the token, NEVER from the request body.
    account = Account(user_id=user_id, **data.model_dump())
    db.add(account)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ConflictError(DUPLICATE_ACCOUNT_MESSAGE) from None

    log.info("account_created", user_id=str(user_id), account_id=str(account.id))
    return account


def get_account(db: Session, user_id: uuid.UUID, account_id: uuid.UUID) -> Account:
    """One of the user's accounts, or NotFoundError (404).

    "Doesn't exist" and "belongs to someone else" give the SAME 404, so a
    caller can't probe which account ids exist (IDOR protection, see
    NotFoundError). Used by update/delete here and by uploads in Step 7.
    """
    stmt = select(Account).where(Account.id == account_id, Account.user_id == user_id)
    account = db.scalar(stmt)
    if account is None:
        raise NotFoundError(ACCOUNT_NOT_FOUND_MESSAGE)
    return account


def update_account(
    db: Session, user_id: uuid.UUID, account_id: uuid.UUID, data: AccountUpdate
) -> Account:
    """Change the nickname and/or account type of one of the user's accounts.

    PATCH = partial update: only the fields the client sent are changed.
    An explicit null is already rejected by AccountUpdate (422).
    """
    # Ownership check: 404 for a missing OR another user's account.
    account = get_account(db, user_id, account_id)
    # exclude_unset=True: only the keys present in the JSON body, so a field
    # that wasn't sent is left alone instead of being set to None.
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        # Same as `account.nickname = value`, with the name in a variable.
        setattr(account, field, value)
    # One commit after the loop: all fields are saved together, or none.
    db.commit()

    log.info("account_updated", user_id=str(user_id), account_id=str(account_id))
    return account


def delete_account(db: Session, user_id: uuid.UUID, account_id: uuid.UUID) -> None:
    """Delete one of the user's accounts (hard delete).

    From Step 7 on, its uploads and transactions go with it through
    ON DELETE CASCADE foreign keys.
    """
    account = get_account(db, user_id, account_id)
    db.delete(account)
    db.commit()
    log.info("account_deleted", user_id=str(user_id), account_id=str(account_id))
