# ADR 020 — Statement passwords

- Status: accepted
- Date: 2026-11-01 (Phase 2, Step 10)

## Context
Indian banks often email statements as password-protected PDFs. SPEC §6.1 wants
them imported, and CLAUDE.md §7.2 says the password is **never persisted**: not
in the database, not in Redis/RQ job arguments, not in logs or error messages.

Since ADR 013 the import runs in an RQ worker, and a job's only argument is the
upload id. The worker therefore can never get the password, because job arguments
sit in Redis in plain text. SPEC §6.1 left two options open: unlock the PDF in the
request and store an unlocked copy, or extract the words during the request.
The Phase 2 plan picked the first, with `pypdf`. This ADR records the details.

## Decision

**1. The lock comes off in the API request, with pypdf.**
`app/parsers/pdf_unlock.py` (`unlock_pdf`) decrypts the PDF in memory and writes
it out again without encryption (`PdfWriter(clone_from=reader)`). The unlocked
copy is then stored like every upload: encrypted with our key (AES-256-GCM,
ADR 012). The worker always reads an ordinary PDF, and the password exists only
as a local variable of one request. pdfplumber, the reader, can open a locked
PDF too, but it cannot save an unlocked copy.

**2. Two ways in, as SPEC §6.1 and §10 describe.**

| Request | Result |
|---|---|
| `POST /uploads` with `password` | unlocked now, stored, queued: **202** |
| `POST /uploads` without it | stored **locked** as `needs_password`, not queued: **409 `PASSWORD_REQUIRED`** with `details.upload_id` |
| `POST /uploads/{id}/password` | unlocked, the stored file replaced, `pending`, queued: **202** |
| wrong password on `POST /uploads` | **400 `INCORRECT_PASSWORD`**, nothing stored; the user sends the upload again |
| wrong password on `/password` | **400 `INCORRECT_PASSWORD`**, still `needs_password`; the user tries again |
| the same locked file again while it waits | **409 `PASSWORD_REQUIRED`** pointing at the waiting upload. A password sent along is **not** used |

`PASSWORD_REQUIRED` is a 409 (SPEC §6.1) with its own code, so the frontend can
tell it apart from "already uploaded" (`CONFLICT`). A wrong password is 400 and
not 401: a 401 means "your access token", and a client would refresh or log out.

**3. An empty password is always tried first.** A PDF locked with only an
owner password (it restricts printing, but anyone can open it) is unlocked
without asking the user.

**4. A PDF that pypdf cannot open is passed on unchanged.** Judging a damaged
file is the worker's job (ADR 013), and pdfplumber reads some files pypdf
refuses. If such a file turns out to be locked, the worker fails it with "this
PDF is password-protected and could not be unlocked".

**5. Keeping the password out of everything.**
- It travels as a pydantic `SecretStr` from the route to the moment
  `unlock_pdf` is called, so printing it by mistake shows `**********`.
- The form field and the JSON body are capped at 128 characters. The 422
  handler never echoes input.
- Exceptions raised while handling a pypdf error are raised `from None`, so no
  third-party error text travels with them.
- `POST /uploads/{id}/password` is limited to **5/minute/user**
  (`UNLOCK_LIMIT`), like login, because every request is one guess.
- Re-uploading a waiting file with a password is deliberately NOT a second
  unlock route. Uploads have their own counter (10/hour), so it would add
  guesses on top of `UNLOCK_LIMIT`.

**6. Lifetime.** A `needs_password` upload is not "stale" (it waits for the user,
not for a job), so the one-hour rule leaves it alone. Its locked file is still
deleted after 24 hours by the Step 3 cleanup, which fails the upload as expired.
Unlocking does not reset that clock. `fail_upload` now also accepts
`needs_password` (`NOT_FINAL_STATUSES`). If our stored copy is missing or fails
its GCM check when the password arrives, the upload is failed (409): no password
can help.

## Alternatives rejected
- **Extract the words during the request** (the SPEC's other option). It puts
  the slow part back into the HTTP request, and the worker would have to read a
  custom format of ours instead of a PDF, so the `.xls`/PDF/CSV pipeline would
  get a fourth input kind.
- **Pass the password to the job.** It is forbidden by §7.2: job arguments are
  stored unencrypted in Redis.
- **Keep the password encrypted with our key until the worker runs.** That
  still persists it, which §7.2 forbids in any form.
- **Use the password sent with a re-upload to unlock the waiting upload.**
  It is friendlier, but it gives one upload two unlock routes, one of them
  outside `UNLOCK_LIMIT`.
- **pikepdf/qpdf instead of pypdf.** It is faster, but it is a compiled
  dependency for a step that runs once per upload on files of a few hundred KB.
  pypdf is pure Python and uses the `cryptography` package we already have
  for AES.

## Consequences
- New runtime dependency `pypdf`. The worker image must be rebuilt
  (`docker compose up -d --build worker scheduler`), because it imports the
  service that imports pypdf.
- The stored file of an unlocked PDF is pypdf's rewrite, not the bank's bytes.
  Tests check that it reads **identically**: every word's text, page and
  position, for all five pypdf encryption methods, for a file encrypted by fpdf2,
  and locally for the real ICICI PDF (`tests/unit/test_real_samples.py`, RC4-128,
  AES-128 and AES-256). No real password-protected statement has been seen yet.
  The first one should be checked when it arrives.
- `file_sha256` stays the hash of the file **as uploaded** (the locked one), so
  sending the same locked file again is recognised as the same file.
- One test proves the password is never persisted:
  `test_the_password_is_never_stored_logged_or_echoed` runs the whole flow (no
  password, a wrong one, the right one), then searches every response, every raw
  structlog event, every stdlib log record, every row of every table, the stored
  file, and every value in Redis (job data decompressed) for both passwords. Each
  of the log, Redis and database checks was confirmed to fail on a deliberately
  planted leak.

## Amendment — code review of PR 25 (10 findings, all addressed)

1. **Untrusted PDF work in the request.** Measured (synthetic files, this
   machine): a 9 MB, 1000-page statement unlocks in 0.27 s; a 5.5 MB file of
   100 000 tiny objects in 2.3 s; a 23 MB file of 400 000 objects is refused by
   pypdf's own parsing budget (`LimitReachedError`) after 3.6 s. The real ICICI
   PDF has 81 objects. The request cost is therefore bounded by the upload limit
   (10 MB) and pypdf's budgets, and no timeout was added. A worker process with
   a hard timeout was considered and rejected for now, because it adds a process
   pool to the API for a few seconds of worst case. What changed:
   - the unlocked copy is size-checked: over `MAX_UPLOAD_MB` is a **413** (the
     upload is failed when it came through `/password`);
   - the unlock no longer holds a row lock or a database connection (point 2).
2. **No row lock during the unlock.** `unlock_upload` now runs as three steps:
   - read and check, then end the transaction;
   - unlock with no transaction open;
   - lock the row, check it is still `needs_password`, then store and enqueue.

   A delete during an unlock now succeeds instead of answering "being imported
   right now". A delete, an expiry or another request's unlock that happens in
   between wins: 404 or 409, and nothing is stored or queued.
3. **`decrypt()` raising is not ours to judge.** `unlock_pdf` now passes a file on
   unchanged when pypdf cannot decrypt it (an unsupported variant, a damaged
   encryption dictionary), not only when it cannot open it. The worker decides,
   as decision 4 above intends. Only a failure to *write* the copy (the password
   was right) is still a 400 in the request.
4. A missing stored file says "no longer available" (`file_missing`), like the
   worker. A damaged one says "could not be read" (`file_unreadable`).
5. The upload that loses the unique-index race now gets the same answer as a
   later duplicate: `PASSWORD_REQUIRED` with the waiting upload's id.
6. `select_owned_upload` (was `_owned`) is the one "this user's upload" query,
   shared by GET, DELETE and the password endpoint.
7. `process_upload` decrypts through the same `_decrypted` helper.
8. Shared test helpers: `held_queue` moved to `tests/integration/conftest.py`,
   `post_upload` takes a `password`, `is_encrypted` lives in the PDF fixtures.
9. `unlock_pdf` parses the file once: one reader, `decrypt("")` then
   `decrypt(password)` (a test pins one reader and two attempts).
10. `PDF_UNREADABLE_MESSAGE` lives in `readers.py`; `pdf_unlock` imports it, so
    reading a CSV or `.xls` no longer loads pypdf.
