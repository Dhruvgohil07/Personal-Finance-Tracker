# ADR 011 — Upload lifecycle: failed uploads are recorded and do not block a retry

- Status: accepted (supersedes ADR 008)
- Date: 2026-10-15 (Phase 2, Step 1)

## Context
In Phase 1 the import ran inside the `POST /uploads` request. ADR 008 decided
that a failed import writes **no** `statement_uploads` row. The caller already
learned the reason from the response. And because of `unique(user_id,
file_sha256)` (dedupe level 1), a recorded failure would have blocked that file
for good, even after we fixed the parser that failed on it.

Phase 2 moves parsing into an RQ worker (SPEC §6.1). That removes both of ADR
008's premises:

1. The request ends before parsing starts. The row has to exist first (status
   `pending`), because the client polls `GET /uploads/{id}` and a failure has
   nowhere else to be reported. So a failed import necessarily leaves a row
   behind.
2. With that row in place, the unique constraint blocks the retry problem ADR
   008 was avoiding. Something has to make retrying possible again. ADR 008
   itself named the two candidates: `DELETE /uploads/{id}`, or a partial unique
   index that excludes `failed`.

## Decision
**Dedupe level 1 becomes a partial unique index that skips `failed` rows:**

```sql
CREATE UNIQUE INDEX uq_statement_uploads_user_id_file_sha256_not_failed
    ON statement_uploads (user_id, file_sha256)
    WHERE status <> 'failed';
```

(Migration 0006. Postgres does not allow a WHERE clause on a UNIQUE
*constraint*, only on a unique *index*. Both enforce uniqueness the same way.)

- The service's "already uploaded?" check (`_already_uploaded` in
  `app/services/imports.py`) uses the **same condition**, literally the same
  SQL: one constant, `NOT_FAILED_SQL` in `app/models/statement_upload.py`,
  is both the index's WHERE clause and part of the query. So the friendly 409
  and the database agree on what a duplicate is. It is literal SQL, not
  `status != :param`, because of how Postgres handles partial indexes. The
  planner may only use one when it can prove the query's condition implies
  the index's. In a prepared (generic) plan, which psycopg switches to after
  5 runs of a query on a connection, a bound parameter's value is unknown, so
  the proof fails and the index is skipped. Measured with a forced generic plan:
  the bound version never touches the partial index, and the literal version
  uses it. Tested in `tests/integration/test_upload_storage.py`. The database is still the
  final word. Two simultaneous uploads of one file both pass the check, and
  the index rejects the second. The service recognises the index by name
  (Postgres reports an index's name in the same `constraint_name` field it uses
  for a constraint) and answers 409, not 500.
- A failed row is **kept**, with its `error_message`. `statement_uploads`
  becomes a complete record of upload *attempts*, which the Phase 3 upload page
  can show.
- `DELETE /uploads/{id}` is still built (Step 2), for a different purpose: to
  undo a *successful* import that turned out to be wrong (wrong account,
  a statement the user did not mean to add). It is not needed for retries.

The statuses a row moves through from Phase 2 on:

```
pending -> processing -> completed
                      -> needs_review    (imported, but the balances do not add up; Step 4)
                      -> failed          (unreadable, wrong account, unexpected error)
needs_password -> pending                (password-protected PDF; Step 10)
```

Only `failed` frees the file. `needs_review` keeps it blocked on purpose: its
rows were imported, and re-uploading the same bytes would only produce
duplicates.

## Alternatives rejected

**`DELETE /uploads/{id}` as the only way to retry.** It works with the existing
constraint, but it makes the user do the database's bookkeeping. To retry a
file, they first have to find and delete the failed attempt. Otherwise the
upload is refused with "you have already uploaded this file", which is false:
nothing from it was imported. The partial index needs no extra step, and
Delete remains for the case where it is actually the right tool.

**Replace a failed row when the same file is uploaded again** (delete-then-insert
in the upload transaction). This keeps one row per file, but it needs a rule
about which statuses may be replaced and loses the history of attempts, for no
gain over simply allowing a new row.

**Keep the constraint and clear `file_sha256` on failure.** It frees the file,
but destroys the information that the failed attempt was *this* file, and the
CHECK on the column would need a NULL exception. Mutating a key column to dodge
a constraint is the kind of trick that is hard to explain later.

## Consequences
- Retrying after a failure needs nothing from the user or from us. This is
  ADR 008's goal, kept now that failures are recorded.
- One failed file may be followed by any number of failed attempts. The
  10/hour upload rate limit bounds this, and the rows are small.
- Among the non-failed rows the rule is unchanged: a failure frees the file
  for **one** new live upload, not several. Tested in
  `tests/integration/test_upload_storage.py`.
- The index follows **status changes**, not only inserts: when the worker sets
  a `pending` row to `failed`, the file becomes uploadable at that moment.
- The partial index no longer covers *every* row of a user, and the old
  constraint's index had been the only index starting with `user_id`. Without a
  replacement, two queries become sequential scans of every user's uploads: the
  ON DELETE CASCADE from `users` (DELETE /me) and the upload list (Step 2).
  Migration 0007 therefore adds `ix_statement_uploads_user_id_created_at`
  `(user_id, created_at DESC)`, which serves both. The list reads it in order,
  with no sort step. Found in the code review of Step 1.
- `downgrade()` of migration 0006 fails if a user has a failed row and a later
  live row for the same file, which the old constraint forbids. The failed row
  must be deleted first. This is noted in the migration.
