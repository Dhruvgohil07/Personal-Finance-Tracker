"""GET/POST/PATCH/DELETE /api/v1/rules - your own categorization rules (ADR 017).

A rule files every matching transaction under one category, before any
built-in guess. Every change re-categorizes your past transactions in the
background (except the ones you categorized by hand), and future imports
use the rules as they arrive.

Thin routes, like the others: the queue is a dependency so the tests can
swap in a synchronous one, and everything else is in app/services/rules.py.
"""

import uuid
from http import HTTPStatus

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession, JobQueue
from app.core.errors import ErrorResponse
from app.schemas.rule import RuleCreate, RuleResponse, RuleUpdate
from app.services import rules as rules_service

router = APIRouter(prefix="/rules", tags=["rules"])

# Shown in /docs: 404 for a rule or a category that is not yours, 409 for
# a second rule matching the same thing.
_NOT_FOUND = {HTTPStatus.NOT_FOUND: {"model": ErrorResponse}}
_CONFLICT = {HTTPStatus.CONFLICT: {"model": ErrorResponse}}


@router.get(
    "", response_model=list[RuleResponse], summary="List your rules in the order they apply"
)
def list_rules(user: CurrentUser, db: DbSession) -> list[RuleResponse]:
    """Your rules, the one tried first at the top: lowest priority number,
    and among equal numbers the one created or changed most recently."""
    return [RuleResponse.model_validate(rule) for rule in rules_service.list_rules(db, user.id)]


@router.post(
    "",
    status_code=HTTPStatus.CREATED,
    response_model=RuleResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Add a rule",
)
def create_rule(
    user: CurrentUser, db: DbSession, queue: JobQueue, body: RuleCreate
) -> RuleResponse:
    """The pattern is stored normalized: a merchant name as its key
    ("Moon Cafe" -> "MOONCAFE"), a UPI id lowercased, a text uppercased."""
    rule = rules_service.create_rule(db, queue, user.id, body)
    return RuleResponse.model_validate(rule)


@router.patch(
    "/{rule_id}",
    response_model=RuleResponse,
    responses=_NOT_FOUND,
    summary="Change a rule's category or priority",
)
def update_rule(
    user: CurrentUser, db: DbSession, queue: JobQueue, rule_id: uuid.UUID, body: RuleUpdate
) -> RuleResponse:
    rule = rules_service.update_rule(db, queue, user.id, rule_id, body)
    return RuleResponse.model_validate(rule)


@router.delete(
    "/{rule_id}", status_code=HTTPStatus.NO_CONTENT, responses=_NOT_FOUND, summary="Delete a rule"
)
def delete_rule(user: CurrentUser, db: DbSession, queue: JobQueue, rule_id: uuid.UUID) -> None:
    """The transactions it categorized go back to what the other rules and
    the built-in categorizer say."""
    rules_service.delete_rule(db, queue, user.id, rule_id)
