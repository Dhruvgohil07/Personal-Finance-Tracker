"""Small building blocks shared by several request schemas."""

from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

# For StringConstraints(pattern=...): no control characters, i.e. none of
# U+0000-U+001F (NUL, tab, newline, ...) or U+007F (DEL).
# A NUL byte can't be stored in a Postgres text column at all: the driver
# raises an error on INSERT, which would surface as a 500. Tabs and newlines
# have no place in a one-line label either. With this pattern they become a
# normal 422 instead.
# (Lone surrogates like "\ud800" are already rejected by Pydantic's JSON
# parser, so they never reach our schemas.)
NO_CONTROL_CHARS = r"^[^\x00-\x1f\x7f]*$"


class QueryFilters(BaseModel):
    """Base class for the *query parameter models* of our GET endpoints.

    FastAPI can fill one Pydantic model from the whole query string
    (`Annotated[MyFilters, Query()]`), which is how `GET /transactions` and
    the insights endpoints document and validate their parameters in one
    place instead of as a row of loose route arguments. Two rules are the
    same for every such model, so they live here:

    **An unknown parameter is a 422** (`extra="forbid"`). Without it,
    `?catgory_id=...` would be silently ignored and the endpoint would
    cheerfully return everything - a typo that looks like a bug in the API.

    **A blank value means "not sent"** (the validator below).

    Subclasses add their own fields and their own cross-field checks; they
    should not repeat either of these.
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _blank_filters_mean_not_sent(cls, data: Any) -> Any:
        """Treat `?from=&direction=&q=` as "no filters", for EVERY field.

        A client that builds the query string from all of its filter inputs
        sends an empty value for each one the user left alone - an HTML form
        does it, and so does `URLSearchParams` over a form state. Without
        this, `?q=` is "no search" (an empty string is falsy) while `?from=`
        is a 422 ("input is too short") and `?direction=` a 422 ("should be
        'debit' or 'credit'"): the same request rejected inconsistently,
        depending on which filter happened to be left blank.

        A blank value carries no filtering intent, so the key is dropped
        and the field falls back to its default - which for a paging
        `limit` is the default page size, not an error.

        Only the model's OWN parameter names are dropped, so a misspelled
        parameter is still a 422 even when it arrives blank (see
        `extra="forbid"` above).
        """
        if not isinstance(data, dict):
            return data
        known = {field.alias or name for name, field in cls.model_fields.items()}
        return {
            key: value
            for key, value in data.items()
            if not (key in known and isinstance(value, str) and not value.strip())
        }
