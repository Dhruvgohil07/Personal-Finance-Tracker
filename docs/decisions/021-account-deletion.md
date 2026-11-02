# ADR 021 — Account deletion

- Status: accepted
- Date: 2026-11-02 (Phase 3, Step 1)

## Context
CLAUDE.md §7.3: *"`DELETE /me` performs a hard delete of all user data (cascade)
and writes a final audit entry."* The frontend's Settings page (Step 10) needs it,
and so does anyone who tries the app and wants their bank data gone afterwards.

Three questions had to be settled: how "everything" is found, how a stolen session
is kept from deleting someone, and how the record of the deletion can survive it.

## Decision

**1. One `DELETE FROM users`, and the database cascades.**
Every table holding a user's data already has a foreign key to `users` with
`ON DELETE CASCADE` (SPEC §5, kept since migration 0002): accounts, uploads and
their blobs, transactions, rules, the user's own categories and refresh tokens.
`app/services/me.py` runs one bulk `delete(User)` statement. There is no list of
tables in the code that could fall behind the schema.
`tests/integration/test_me.py` walks **every table in the models** and checks
that the user has zero rows left, another user's rows are untouched, and nothing
else went. A table it does not know how to check fails the test, so a new table
cannot be added without deciding how deletion covers it.

**2. The current password is asked for again.**
`DELETE /me` takes `{"password": "..."}`. An access token alone (a laptop left
unlocked, a token leaked from the page) is not enough to wipe someone's data.
- A wrong password is **400 `INCORRECT_PASSWORD`** and changes nothing. Not 401:
  the frontend will treat a 401 as "access token expired" and refresh or log out
  (the same reasoning as ADR 020). Decided with Dhruv at the start of this step;
  the Phase 3 plan had said 401.
- Each request is a password guess, so the route is limited to **5/minute per user**
  (`REAUTH_LIMIT`, see the ADR 007 amendment).

**3. Every session ends.** Deleting the user deletes their refresh tokens, so the
old cookie matches no row and `/auth/refresh` answers 401. An access token that
is still within its 15 minutes is refused by `get_current_user`, which looks the
user up. The response also clears the refresh cookie. Its path is
`/api/v1/auth`, but a response from `/api/v1/me` may still clear it: only the
cookie's attributes must match, not the URL that sends the header.

**4. The audit row has no foreign key and no email.**
Migration 0013 adds `audit_log` (SPEC §5). Its `user_id` has **no foreign key**:
with one, the database would either refuse the delete or cascade and remove the
very row that records it. The row is added in the **same transaction** as the
delete, so there is never a deletion without a record, or a record without a
deletion. It holds the user id, `action = user_deleted`, `entity = user`, the
client address and the User-Agent (cut to 512 characters). No email, name or
financial value, so after the delete nothing in the database says who the user
was.

The action is called `user_deleted`, not SPEC §5's example `account_deleted`,
because in this codebase an "account" is a bank account.

**5. Work in flight.** A running import locks the user row first
(`app/services/imports.py`, "Lock order"), so the DELETE waits for it to commit
and then removes its rows too. A job still waiting in the queue later finds its
upload gone and does nothing (tested).

## Alternatives rejected
- **Deleting table by table in the service.** A list of tables in Python would
  silently miss the next table added; the cascade cannot.
- **Soft delete (a `deleted_at` column).** The data would still exist, which is
  the opposite of what the user asked for, and every query would need a filter.
- **A foreign key on `audit_log.user_id` with `ON DELETE SET NULL`.** The row
  would survive, but would no longer say whose deletion it records.
- **`POST /me/delete` instead of a DELETE with a body.** SPEC §10 names
  `DELETE /me`; `fetch()` and the Next.js proxy both pass a DELETE body on.
- **No password, only the access token.** Simpler, but turns any stolen session
  into a way to destroy the account.

## Consequences
- The audit row keeps an IP address and a User-Agent after the user is gone. They
  are kept as the security record of who asked for the deletion, and nothing else
  about the person remains to link them to.
- `audit_log` has no index besides its primary key: nothing reads it yet. The first
  feature that reads it adds the index its query needs.
- A new audited action is a new `AuditAction` member plus a migration that widens
  the CHECK constraint (ADR 002).
- `client_host()` in `app/core/rate_limit.py` is now the one place that decides the
  client's address, for both the rate limits and the audit log. Step 2 (client IP
  behind the proxy) changes it there, and both follow.
