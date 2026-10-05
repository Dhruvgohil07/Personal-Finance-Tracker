"""POST /api/v1/uploads - upload a bank statement and import it.

This is the only endpoint in the app that takes a FILE, so it is the only
one using a multipart form instead of a JSON body:

    file           the statement (.xls or .csv; PDF is Phase 2)
    account_id     which of your accounts it belongs to
    column_mapping optional JSON, only for a CSV no bank parser recognises

In Phase 1 the import runs inside the request (SPEC §16), so the response
already contains the result: how many rows were parsed, inserted and
skipped as duplicates. Phase 2 moves the work to an RQ worker, and this
route will then answer 202 with a `pending` upload that the client polls.

The route itself stays thin, like the others: read the form, turn it into
plain Python values, call the service, return a response model.
"""

import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.api.deps import CurrentUser, DbSession
from app.core.config import get_settings
from app.core.errors import ErrorResponse, PayloadTooLargeError
from app.core.rate_limit import UPLOAD_LIMIT, limiter
from app.parsers.generic_csv import ColumnMapping
from app.schemas.upload import ColumnMappingRequest, UploadResponse, column_mapping_json_example
from app.services import imports as imports_service

router = APIRouter(prefix="/uploads", tags=["uploads"])

# Shown in /docs for the statuses this route can answer.
_RESPONSES = {
    HTTPStatus.BAD_REQUEST: {"model": ErrorResponse},
    HTTPStatus.NOT_FOUND: {"model": ErrorResponse},
    HTTPStatus.CONFLICT: {"model": ErrorResponse},
    HTTPStatus.REQUEST_ENTITY_TOO_LARGE: {"model": ErrorResponse},
    HTTPStatus.TOO_MANY_REQUESTS: {"model": ErrorResponse},
}

# How much is read from the upload at a time while checking the size limit.
_READ_CHUNK_BYTES = 64 * 1024


@router.post(
    "",
    status_code=HTTPStatus.CREATED,
    response_model=UploadResponse,
    responses=_RESPONSES,
    summary="Upload a bank statement",
)
# 10 uploads per hour per user (SPEC §7.1). Parsing is the most expensive
# thing the API does, so this limit protects the server as well as the user.
# The limiter's default key is the user id from the access token
# (app/core/rate_limit.py), so no key_func is needed here.
@limiter.limit(UPLOAD_LIMIT)
def create_upload(
    request: Request,
    user: CurrentUser,
    db: DbSession,
    # File() and Form() tell FastAPI this is a multipart form, not JSON.
    file: Annotated[UploadFile, File(description="The statement file (.xls or .csv)")],
    account_id: Annotated[uuid.UUID, Form(description="The account this statement belongs to")],
    column_mapping: Annotated[
        str | None,
        Form(
            description=(
                "Only for a CSV that no bank parser recognises: a JSON object naming "
                "the columns. Example: " + str(column_mapping_json_example())
            )
        ),
    ] = None,
) -> UploadResponse:
    """Import a statement into one of your accounts.

    Answers 400 if the file cannot be read or does not belong to the chosen
    account, 404 if the account is not yours, 409 if you have uploaded this
    exact file before, and 413 if it is over the size limit.
    """
    content = _read_within_limit(file)
    upload = imports_service.import_statement(
        db,
        user.id,
        account_id=account_id,
        # The browser may send no filename at all; the service sanitizes
        # whatever does arrive before storing it.
        filename=file.filename or "statement",
        content=content,
        column_mapping=_column_mapping_from_json(column_mapping),
    )
    return UploadResponse.model_validate(upload)


def _read_within_limit(file: UploadFile) -> bytes:
    """Read the whole upload into memory, stopping at `MAX_UPLOAD_MB`.

    Read in chunks, not with one `.read()`: an oversized file is refused
    after the first chunk over the limit instead of being fully loaded
    first.

    `file.file` is the synchronous file object behind `UploadFile`. This
    route is a normal `def`, which FastAPI runs in a worker thread, so
    blocking reads here do not block the event loop - the same reason the
    database calls can be synchronous.

    Note for Phase 2: Starlette buffers a multipart body over ~1 MB into a
    temporary file before this function runs, so a large upload does touch
    the disk briefly (it is deleted when the request ends). Rejecting it
    before that needs a Content-Length check in middleware; see
    docs/progress.md.
    """
    limit = get_settings().max_upload_bytes
    chunks: list[bytes] = []
    total = 0
    while chunk := file.file.read(_READ_CHUNK_BYTES):
        total += len(chunk)
        if total > limit:
            raise PayloadTooLargeError(
                f"The file is larger than the {get_settings().max_upload_mb} MB upload limit."
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _column_mapping_from_json(raw: str | None) -> ColumnMapping | None:
    """Validate the `column_mapping` form field and convert it.

    It arrives as a STRING, because a multipart form has no nested JSON.
    So the string is validated with the Pydantic model by hand, and its
    errors are re-raised as the `RequestValidationError` FastAPI would have
    raised for a JSON body - which our existing handler turns into the
    standard 422 with a list of fields (app/core/errors.py).

    The field path is prefixed with `body.column_mapping`, so the response
    says `body.column_mapping.date_format` rather than just `date_format`.
    `include_input=False` keeps the value the user sent out of the error,
    the same rule the global handler follows.
    """
    if raw is None or not raw.strip():
        return None
    try:
        mapping_request = ColumnMappingRequest.model_validate_json(raw)
    except ValidationError as exc:
        raise RequestValidationError(
            [
                {**error, "loc": ("body", "column_mapping", *error.get("loc", ()))}
                for error in exc.errors(include_input=False, include_url=False)
            ]
        ) from None
    return mapping_request.to_mapping()
