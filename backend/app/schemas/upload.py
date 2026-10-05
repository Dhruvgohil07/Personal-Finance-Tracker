"""Request and response models for the /uploads endpoints.

`ColumnMappingRequest` is the API edge of `ColumnMapping`
(`app/parsers/generic_csv.py`). There are two classes for one idea on
purpose, and this is the dependency direction the project follows:

    API (Pydantic, untrusted JSON, 422 with field names)
      -> parsers (frozen dataclass, pure, no FastAPI, no SQLAlchemy)

The parser package must stay testable with nothing but the standard
library, so it cannot import Pydantic models; and a Pydantic model is what
gives the user a proper 422 naming the field they got wrong, plus the
documentation in /docs. So the rules are stated twice: once here for the
user, once in the dataclass as the backstop for our own code. A test keeps
the two in step.
"""

import uuid
from datetime import date, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models import UploadStatus
from app.parsers.base import FileType
from app.parsers.generic_csv import ColumnMapping
from app.schemas.common import NO_CONTROL_CHARS

# A column header as it appears in the CSV ("Narration", "Withdrawal Amt.").
# Control characters are rejected here (see app/schemas/common.py); the
# length cap is generous but finite, because this value is compared against
# every header cell of the file.
ColumnLabel = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=100, pattern=NO_CONTROL_CHARS
    ),
]
# A strptime pattern such as "%d/%m/%Y". Checked for real below.
DateFormat = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=2, max_length=40, pattern=NO_CONTROL_CHARS),
]

# The date used to check that a date_format can express a whole date.
_PROBE_DATE = date(2026, 8, 1)


class ColumnMappingRequest(BaseModel):
    """Which column is which, for a CSV no bank parser recognises.

    Sent with the upload as a JSON string in the multipart form. It is used
    ONLY when no bank parser recognises the file (SPEC §6.2) - a real ICICI
    statement is always read by the ICICI parser.

    Give EITHER `debit_column` and `credit_column` (two columns, each with
    a positive number), OR `amount_column` (one column where a negative
    number, `Dr`, or brackets mean money out).
    """

    model_config = ConfigDict(extra="forbid")

    date_column: ColumnLabel
    description_column: ColumnLabel
    # No default: "01/08/2026" is 1 August in India and 8 January in the
    # USA, and guessing would silently corrupt every date in the file.
    date_format: DateFormat = Field(
        description='strptime pattern, e.g. "%d/%m/%Y" for 01/08/2026 or "%d-%b-%Y" for 01-Aug-2026'
    )
    debit_column: ColumnLabel | None = None
    credit_column: ColumnLabel | None = None
    amount_column: ColumnLabel | None = None
    balance_column: ColumnLabel | None = None

    @model_validator(mode="after")
    def _check_date_format(self) -> "ColumnMappingRequest":
        """Reject a date format that cannot round-trip a whole date.

        The check is: print a known date with the pattern, then read it
        back with the same pattern and see whether the same date comes out.

            "%d/%m/%Y" -> "01/08/2026" -> 2026-08-01   same date, accepted
            "%d/%m"    -> "01/08"      -> 1900-08-01   year lost, rejected
            "%q"       -> "%q"         -> ValueError    not a directive

        Without it, a bad pattern is only discovered while parsing, and
        every row then fails with "date is not in the expected format" -
        which blames the statement for a mistake in the request. Here it is
        a 422 naming `date_format`.
        """
        try:
            printed = _PROBE_DATE.strftime(self.date_format)
            parsed = datetime.strptime(printed, self.date_format).date()
        except (ValueError, TypeError):
            raise ValueError("is not a valid date format (e.g. %d/%m/%Y)") from None
        if parsed != _PROBE_DATE:
            raise ValueError("must contain the day, month and year (e.g. %d/%m/%Y)")
        return self

    @model_validator(mode="after")
    def _check_amount_columns(self) -> "ColumnMappingRequest":
        """Mirror `ColumnMapping`'s rules so the user gets a 422, not a 500.

        Building the dataclass is the check itself: it raises `ValueError`
        with exactly these messages, and a `ValueError` raised in a
        validator becomes our standard 422 response. Writing the rules out
        again here would let the two drift apart.
        """
        self.to_mapping()
        return self

    def to_mapping(self) -> ColumnMapping:
        """Convert into the frozen dataclass the parser takes."""
        return ColumnMapping(
            date_column=self.date_column,
            description_column=self.description_column,
            date_format=self.date_format,
            debit_column=self.debit_column,
            credit_column=self.credit_column,
            amount_column=self.amount_column,
            balance_column=self.balance_column,
        )


class UploadResponse(BaseModel):
    """What POST /uploads returns: the upload row and what the import did.

    `rows_parsed = rows_inserted + rows_duplicate`. A re-uploaded
    overlapping statement therefore shows a high `rows_duplicate` and only
    the genuinely new rows in `rows_inserted` (SPEC §6.3).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    original_filename: str
    file_type: FileType
    status: UploadStatus
    period_start: date | None
    period_end: date | None
    rows_parsed: int
    rows_inserted: int
    rows_duplicate: int
    # Always 0 in Phase 1: balance reconciliation is Phase 2 (SPEC §6.4).
    reconciliation_errors: int
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None

    # `file_sha256` is deliberately not part of this response: it is an
    # internal dedupe key, and the client already has the file it sent.


def column_mapping_json_example() -> dict[str, Any]:
    """An example mapping, shown in /docs next to the form field."""
    return {
        "date_column": "Date",
        "description_column": "Narration",
        "date_format": "%d/%m/%Y",
        "debit_column": "Withdrawal Amt.",
        "credit_column": "Deposit Amt.",
        "balance_column": "Closing Balance",
    }
