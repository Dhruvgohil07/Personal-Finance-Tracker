"""Helpers for reading database errors."""

from sqlalchemy.exc import DBAPIError, IntegrityError

# Postgres error code (SQLSTATE) for "deadlock detected".
DEADLOCK_SQLSTATE = "40P01"


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


def is_deadlock(exc: DBAPIError) -> bool:
    """True if Postgres aborted this transaction to break a deadlock.

    Two transactions deadlock when each waits for a row lock the other
    holds. Postgres notices (after `deadlock_timeout`, 1 s by default),
    aborts ONE of them with SQLSTATE 40P01 and lets the other finish. The
    aborted one did nothing, so running it again is safe and normally
    succeeds, because the other transaction has finished by then.

    `exc.orig` is the original psycopg error; psycopg puts the SQLSTATE on
    it as `sqlstate`.
    """
    return getattr(exc.orig, "sqlstate", None) == DEADLOCK_SQLSTATE
