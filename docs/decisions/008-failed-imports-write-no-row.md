# ADR 008 — A failed import writes no `statement_uploads` row (Phase 1)

- Status: accepted
- Date: 2026-10-05 (Phase 1, Step 7)

## Context
`statement_uploads.status` has six values, taken from SPEC §5: `pending`,
`processing`, `needs_password`, `needs_review`, `completed`, `failed`. They
describe the Phase 2 design, where `POST /uploads` only stores the file and
an RQ worker parses it later (SPEC §6.1): the row has to exist first,
because the client polls `GET /uploads/{id}` to learn how it went, and a
failure has nowhere else to be reported.

Phase 1 imports synchronously inside the request (SPEC §16, Phase 1 plan).
That changes the situation in two ways:

1. The caller already learns the outcome from the response, so a row is not
   needed to communicate a failure.
2. `unique(user_id, file_sha256)` (dedupe level 1, ADR 006) means any row
   recorded for a file blocks that file from ever being uploaded again by
   that user.

Point 2 is the problem. Most import failures in this phase are *our* fault
or *not yet implemented*:

- PDF statements are refused until Phase 2;
- a bank layout no parser recognises yet;
- a parser bug on a narration we had not seen.

If each of those wrote `status = failed`, then after we add PDF support or
fix the parser, the user's file would still be rejected with "you have
already uploaded this file" — and Phase 1 has no `DELETE /uploads/{id}` to
clear it (deferred to Phase 2).

## Decision
In Phase 1, a failed import writes **nothing**. The request answers with
the reason and the database is left untouched:

| Failure | Response |
|---|---|
| over `MAX_UPLOAD_MB` | 413 `PAYLOAD_TOO_LARGE` |
| account unknown or someone else's | 404 `NOT_FOUND` |
| this exact file already imported | 409 `CONFLICT` |
| unreadable file, PDF, unknown layout, wrong account number | 400 `INVALID_STATEMENT` |

So `statement_uploads` holds only `completed` rows, and the other five
statuses stay unused until Phase 2. `error_message`, `reconciliation_errors`
and the status enum are created now anyway, so Phase 2 needs no migration
(the same reasoning as ADR 002).

Everything a successful import writes — the upload row and all its
transactions — goes in **one database transaction**: `db.flush()` makes the
upload's id available, the transactions are inserted with
`ON CONFLICT DO NOTHING`, the counts are written back, and only then is the
transaction committed. There is no state in which an upload row exists with
half of its rows.

## Alternatives rejected

**Record every failure as `status = failed`.** The honest audit trail, and
what Phase 2 will do. Rejected here because of the retry problem above: the
most likely failures in Phase 1 are ones we intend to fix, and a user who
cannot re-upload their statement afterwards has been punished for our bug.
A failure is not lost either way — it is logged, with the reason and no
file contents (rule 3).

**Record the failure, and let a re-upload replace a failed row.** Keeps the
audit trail and allows retries. Rejected as the wrong complexity for this
phase: it means a delete-then-insert inside the import transaction, plus a
rule about which statuses may be replaced, to support a `GET /uploads` list
that Phase 1 does not have yet.

**Exclude failed rows from the unique constraint** (a partial unique index
`WHERE status <> 'failed'`). Technically neat, and worth revisiting in
Phase 2. Rejected now because the constraint is the single mechanism
dedupe level 1 relies on, and narrowing it to support rows that Phase 1
never writes adds a migration and a subtlety for no present benefit.

## Consequences
- A user can retry a file as soon as the reason it failed is fixed, with no
  intervention from us.
- `statement_uploads` is not a complete record of upload *attempts* in
  Phase 1. The logs are: every import logs ids, the file type and counts,
  and a refused one is visible as a 4xx in the request log.
- Phase 2 must revisit this. When the worker takes over, the row is created
  before parsing and the unused statuses come into play; at that point
  either `DELETE /uploads/{id}` (already planned) or a partial unique index
  has to make retrying possible again.
- Tests assert the absence: a rejected PDF leaves `statement_uploads`
  empty (`tests/integration/test_uploads.py`).
