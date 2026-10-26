# ADR 013 — Uploads are imported by an RQ worker

- Status: accepted
- Date: 2026-10-25 (Phase 2, Step 2)

## Context
In Phase 1, `POST /uploads` parsed the file and inserted the transactions inside
the request, and answered `201` with the final counts. SPEC §6.1 and §11 move
that work into an RQ worker: the request stores the file and the worker imports
it. Phase 2 makes an import slower (PDFs, reconciliation, categorization), and
a worker can retry after a database blip, which a request cannot.

That raises questions the SPEC leaves open:

1. Which checks still happen in the request, and which move to the worker?
2. How does the worker get what it needs, when a job's only argument is the
   upload id (SPEC §11)? A generic CSV also needs its `column_mapping`.
3. When does a retry happen, and when is the upload marked `failed`?
4. How is the worker started, given that RQ needs `fork()` (not on Windows)?

## Decision

**1. Cheap checks in the request, parsing in the worker.**
`create_upload` (API side) checks the size (413), the account's ownership (404),
the file's sha256 (409, dedupe level 1) and the file type from its signature
(400; a PDF is refused here until Step 9). It then commits the `pending` row and
the AES-256-GCM-encrypted file (ADR 012) **together**, and only then enqueues the
job, so the worker can never start before both exist. It answers **202** with
the `pending` upload. Everything that needs the file parsed (an unreadable row,
an unknown layout, a statement for another account) is found by the worker and
recorded as `failed` with the reason in `error_message`. The client polls
`GET /uploads/{id}`.

**2. The job gets the upload id. Everything else comes from the database.**
`process_statement(upload_id: str)` is enqueued with exactly one argument. The
file is read from `upload_blobs`. The column mapping is stored on the upload row,
in the new nullable JSONB column `statement_uploads.column_mapping` (migration
0008). It holds header labels and a date pattern, nothing from the rows. Jobs are
enqueued by **dotted name** (`"app.workers.jobs.process_statement"`), so the API
never imports worker code: `jobs.py` imports the import service, and the service
enqueues, so a function reference would be a circular import. A unit test checks
that the names resolve.

**3. Two kinds of failure.**
- *Expected* (a file we cannot parse, the wrong account, a missing or damaged
  blob): the service marks the upload `failed` and the job **returns normally**.
  RQ does not retry, because the same bytes give the same answer.
- *Unexpected* (anything else): the job raises, and RQ retries it
  (`Retry(max=3, interval=[10, 60, 300])`). The `on_failure` callback marks the
  upload `failed` with a generic message, but **only when `job.should_retry` is
  false**. RQ 2.12 calls `on_failure` after *every* failed attempt, before it
  decides whether to retry (`rq/worker/base.py`). The Phase 2 plan had assumed
  it ran only on the final failure. Without that check, the first transient error
  would fail an upload whose retry was already scheduled.

`process_upload` (worker side) runs in **two transactions**:
1. *claim*: lock the row (`FOR UPDATE`), accept only `pending` or `processing`,
   set `processing`, commit. That makes `processing` visible to `GET` and makes
   `DELETE` answer 409.
2. *import*: lock the row again and re-check that it is still `processing`, then
   decrypt, parse, insert, delete the blob and set `completed`, all in one
   commit. A crash rolls this whole step back and leaves the row `processing`
   for the retry.

This makes the job **idempotent**. A finished or deleted upload is skipped, and
a job enqueued twice waits for the first run's lock, then finds the upload
finished.

If **enqueueing fails** (Redis down), the committed row would stay `pending`
forever and block its file. So it is marked `failed` (deleting the blob), and
the client gets **503**.

**4. `python -m app.workers.worker`, in Docker.**
The SPEC suggests RQ's `rq worker` command. That would run the jobs without our
`configure_logging`, i.e. without the structlog processor that redacts sensitive
fields (CLAUDE.md rule 3). `app/workers/worker.py` configures logging, then does
what `rq worker` does: builds a `Worker` on its own Redis client and calls
`work(with_scheduler=True)`. The scheduler is what moves a delayed retry back
onto the queue. It runs in a `worker` service in docker-compose.yml, built from
`backend/Dockerfile`, because RQ forks a child process per job.

## Alternatives rejected

**Parse in the request, store only the result.** Keeps the immediate 400, but it
is exactly the slow, unretryable request this step exists to remove.

**Read and detect the parser in the request, parse in the worker.** It would
catch "unknown layout" early. But it reads the whole file in the request anyway
(most of the cost), and it splits the parser selection over two processes that
would have to agree.

**Pass the column mapping as a job argument.** No migration needed. But it breaks
the SPEC's "upload id only" rule, which the password tests of Step 10 rely on.
The mapping is harmless, but a rule with exceptions is harder to check.

**Keep the mapping on `upload_blobs`.** Its lifetime would match the file's. But
once the import is over, nothing would show how a CSV was read, which is the
first question when a CSV import looks wrong.

**One transaction for the whole job, `processing` never committed.** Simpler,
but then nobody ever sees `processing`. `DELETE` could not tell a running import
from a queued one, and a crash would leave no trace of having started.

**`rq worker` with a logging config file.** RQ's CLI can load a settings module.
But structlog is configured in Python, and a second way to configure it is one
more thing that can drift from the API's.

## Consequences
- **API contract change:** `POST /uploads` answers `202` with `status: "pending"`
  and zero counts, instead of `201` with the result. Parse errors and the
  wrong-account check no longer give a 400. They appear as `failed` +
  `error_message` on `GET /uploads/{id}`. New endpoints: `GET /uploads`,
  `GET /uploads/{id}`, `DELETE /uploads/{id}` (409 while `processing`).
- Uploads now need Redis **and** a running worker:
  `docker compose up -d --build worker`. Without a worker, uploads stay `pending`.
  Step 3's cleanup job will fail `pending`/`processing` rows older than an hour.
- The worker must use the API's `FILE_ENCRYPTION_KEY`. Compose passes the same
  `.env` to it.
- Tests run jobs synchronously (`Queue(is_async=False)`) on a separate queue
  name. RQ's retries are therefore not exercised end to end. The callback's two
  branches are tested directly, and the retry options are read back from a job
  stored in Redis.
- RQ keeps a failed job's traceback in Redis for 7 days (`failure_ttl`),
  instead of RQ's default of a year.
- The later Phase 2 migrations and ADRs move up by one (see the Phase 2 plan's
  change log).
