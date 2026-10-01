"""GET/POST/PATCH/DELETE /api/v1/accounts.

Every route depends on `CurrentUser`, so a request without a valid access
token never reaches them (401). The routes pass `user.id` to the service,
which scopes every query by it.
"""

import uuid
from http import HTTPStatus

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.core.errors import ErrorResponse
from app.schemas.account import AccountCreate, AccountResponse, AccountUpdate
from app.services import accounts as accounts_service

router = APIRouter(prefix="/accounts", tags=["accounts"])

# Shown in /docs for routes that can answer 404 / 409.
_NOT_FOUND = {HTTPStatus.NOT_FOUND: {"model": ErrorResponse}}
_CONFLICT = {HTTPStatus.CONFLICT: {"model": ErrorResponse}}


@router.get("", response_model=list[AccountResponse])
def list_accounts(user: CurrentUser, db: DbSession) -> list[AccountResponse]:
    accounts = accounts_service.list_accounts(db, user.id)
    return [AccountResponse.model_validate(account) for account in accounts]


@router.post(
    "", status_code=HTTPStatus.CREATED, response_model=AccountResponse, responses=_CONFLICT
)
def create_account(user: CurrentUser, db: DbSession, body: AccountCreate) -> AccountResponse:
    account = accounts_service.create_account(db, user.id, body)
    return AccountResponse.model_validate(account)


# `account_id: uuid.UUID` in the path: FastAPI rejects a non-UUID like
# /accounts/abc with a 422 before our code runs.
# PATCH (not PUT): the client sends only the fields it wants to change.
@router.patch("/{account_id}", response_model=AccountResponse, responses=_NOT_FOUND)
def update_account(
    user: CurrentUser, db: DbSession, account_id: uuid.UUID, body: AccountUpdate
) -> AccountResponse:
    account = accounts_service.update_account(db, user.id, account_id, body)
    return AccountResponse.model_validate(account)


# 204 No Content: success, and there is nothing to send back.
@router.delete("/{account_id}", status_code=HTTPStatus.NO_CONTENT, responses=_NOT_FOUND)
def delete_account(user: CurrentUser, db: DbSession, account_id: uuid.UUID) -> None:
    accounts_service.delete_account(db, user.id, account_id)
