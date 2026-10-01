"""Helpers for reading database errors."""

from sqlalchemy.exc import IntegrityError


def violates_constraint(exc: IntegrityError, constraint_name: str) -> bool:
    """True if this IntegrityError was caused by the named constraint.

    IntegrityError covers several different failures: unique, foreign key,
    CHECK, NOT NULL. A service that turns "duplicate" into a 409 must only do
    that for its own unique constraint; anything else is a bug or a race
    (e.g. the user was deleted mid-request) and should stay a 500.

    `exc.orig` is the original psycopg error. Postgres reports which
    constraint failed in `diag.constraint_name`; our naming convention
    (app/db/base.py) makes those names predictable.
    """
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None) == constraint_name
