"""GET /api/v1/transactions and PATCH /api/v1/transactions/{id}.

The list endpoint is the one the user (and later the frontend) reads the
whole app through, so it is worth seeing how little is in the route: the
filters are a Pydantic model, the paging is in the service, and the route
only converts ORM rows into response models (rule 6, rule 11).

`Annotated[TransactionFilters, Query()]` is FastAPI's *query parameter
model*: instead of ten separate parameters, one Pydantic model collects the
whole query string. The benefits are all in `app/schemas/transaction.py`:
one place to document every filter for /docs, cross-field checks
("from" <= "to") as plain Python, and an unknown parameter rejected
instead of ignored.
"""

import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DbSession
from app.core.errors import ErrorResponse
from app.schemas.transaction import (
    TransactionFilters,
    TransactionPage,
    TransactionResponse,
    TransactionUpdate,
)
from app.services import transactions as transactions_service

router = APIRouter(prefix="/transactions", tags=["transactions"])

# Shown in /docs: both routes answer 404 for an id that is not the
# caller's (a transaction, an account or a category).
_NOT_FOUND = {HTTPStatus.NOT_FOUND: {"model": ErrorResponse}}


@router.get(
    "",
    response_model=TransactionPage,
    responses=_NOT_FOUND,
    summary="List your transactions, newest first",
)
def list_transactions(
    user: CurrentUser, db: DbSession, filters: Annotated[TransactionFilters, Query()]
) -> TransactionPage:
    """One page of your transactions.

    Every filter is optional and they combine with AND. Paging is by
    cursor, not page number: pass the `next_cursor` you were given back as
    `cursor` to get the following page, and stop when it is null.
    """
    page = transactions_service.list_transactions(db, user.id, filters)
    return TransactionPage(
        items=[TransactionResponse.model_validate(row) for row in page.rows],
        next_cursor=page.next_cursor,
    )


@router.patch(
    "/{transaction_id}",
    response_model=TransactionResponse,
    responses=_NOT_FOUND,
    summary="Categorize a transaction or add a note",
)
def update_transaction(
    user: CurrentUser, db: DbSession, transaction_id: uuid.UUID, body: TransactionUpdate
) -> TransactionResponse:
    """Set the category and/or your own note on one transaction.

    Send only the fields you want to change. A category you choose here is
    recorded as `user_manual`, which protects it from being overwritten by
    the automatic categorizer later; sending `null` clears it again.
    """
    transaction = transactions_service.update_transaction(db, user.id, transaction_id, body)
    return TransactionResponse.model_validate(transaction)
