# Phase 2 — Real-world imports

## Goal
Make imports work on real-world statements, not just one known-good file. Phase 2
moves parsing into an RQ worker and stores uploads encrypted. It adds ICICI PDF
statements, including password-protected ones, and checks the balance column. It
records the statement's row order and auto-categorizes transactions (merchant
dictionary, keyword rules, P2P, user rules learned from corrections). It also
detects self transfers between the user's own accounts.

SPEC §16: *"PDFs from all my banks import correctly, overlapping statements merge
cleanly, most transactions auto-categorize."*

### Where Phase 1 left off
- Processing is synchronous: `POST /uploads` parses inside the request.
- Nothing is auto-categorized; every row is `category_source: none`.
- `channel`, `counterparty_vpa`, `merchant_id`, `category_source`,
  `category_confidence` and `is_self_transfer` are never written.
- PDF statements are refused.
- Same-day rows come back in random order, because nothing stores the statement's
  row sequence.

## Done when
- The real ICICI `.xls` **and** PDF import through the worker, and their balances
  reconcile (`completed`, not `needs_review`)
- Overlapping statements, including an `.xls` and a PDF of overlapping periods,
  merge with no duplicates
- A (synthetic) password-protected PDF imports, and tests prove the password is
  never persisted
- A deliberately broken statement lands in `needs_review` with the bad rows listed
- **More than half** of the real statement's transactions auto-categorize without
  an LLM (Phase 4's target is > 85% with it). The measured share is recorded in the
  acceptance doc
- Self transfers between two accounts are detected and excluded from the summary
- `ruff`, `pytest` at **100% coverage (enforced)**, CI green
- Explicitly **not** done: parsers for other banks (no samples, SPEC §6.2)

## Hard constraints
1. **Fingerprints are load-bearing.** `normalize_description` stays byte-for-byte
   unchanged. Channel/VPA/merchant work is **additive**: new fields on
   `NormalizedRow`, kept out of `compute_fingerprint`. A golden test pins today's
   normalized strings and fingerprints *before* any narration work starts.
2. **ADR 008 is superseded.** The worker needs the upload row to exist first
   (`pending`), so a failure has to be recorded without blocking a later retry.
3. **Statement passwords are never persisted** (CLAUDE.md §7.2): not in the DB,
   Redis/RQ job args or logs. Tests assert the job args are `(upload_id,)` only and
   that the serialized job data in Redis does not contain the password.
4. **`app/parsers/` stays pure** (no FastAPI/SQLAlchemy/Redis/structlog/httpx).
   The new `app/categorizer/` package gets the same purity test.
5. **No guessed bank formats** (SPEC §6.2). Only ICICI is built. Other banks wait
   for samples.
6. **RQ needs `fork()`**, so the worker always runs in Docker. Tests run jobs
   synchronously (`Queue(is_async=False)`) on Windows and in CI.

## Decisions made during planning

### Settled with Dhruv
| Question | Answer |
|---|---|
| Phase size | **One Phase 2 with many steps**, not split into 2a/2b |
| New samples | **One ICICI PDF statement** only. No other bank, no password-protected PDF |
| Coverage gate | **Add `fail_under = 100`** to `[tool.coverage.report]` |
| Password-protected PDFs | **Unlock in the request** with `pypdf` (new dependency), then store an unlocked copy app-encrypted with AES-256-GCM. The worker never sees a password. Rejected: extracting the words during the request, which puts the slow part back in the HTTP request and gives the worker a custom format instead of a PDF |
| P2P detection (§8.3) | **Pulled forward from Phase 4** into the Phase 2 categorizer. Most Indian UPI debits are person-to-person, so "most transactions auto-categorize" is hard to meet without it |
| `q=` search / unused trigram index | **Deferred to Phase 3**, when the frontend has a search box |

### Design decisions
| Decision | Choice | Why |
|---|---|---|
| A failed upload vs `unique(user_id, file_sha256)` | Replace the constraint with a **partial unique index `WHERE status <> 'failed'`**, and also build `DELETE /uploads/{id}` | A failure no longer blocks a retry (the reason for ADR 008), with no extra step for the user. Delete is the SPEC's undo button for a bad *successful* import |
| `POST /uploads` response | **202 Accepted** with the `pending` upload; client polls `GET /uploads/{id}` | SPEC §6.1. This changes the Phase 1 contract (201 + final counts) |
| Enqueue fails (Redis down) | Mark the row `failed`, delete the blob, answer **503** | No orphaned `pending` row nobody will ever process |
| Retries | `Retry(max=3)` with back-off for unexpected errors only. A `ParseError`/wrong account is **final at once** (`failed` + message) | Retrying a parse error gives the same answer three times |
| Where categorization runs | **Inside `process_statement`, same DB transaction, right after the insert** | Rows appear already categorized; one less job to explain. Phase 4's LLM step (needs batching/retries) becomes the separate `categorize_upload` job then |
| Self-transfer detection | Separate job **`post_import_analysis(user_id)`**, enqueued after a successful import | It spans all the user's accounts, not one upload. Phase 4's recurring detection joins it |
| Chain finds nothing | `category_id` stays **NULL**, source `none` | "Needs your attention" stays visible as the null bucket. The seeded "Uncategorized" stays the user's explicit "I looked, nothing fits". The insights null bucket is renamed **"Not categorized yet"** so the two no longer share a name |
| Statement row order | New column **`transactions.statement_seq`** (position in its upload, normalized to **chronological**); list order becomes `(txn_date DESC, statement_seq DESC, id DESC)`; cursor gets the third component | Fixes same-day order. Rows of one day from two different uploads have incomparable seqs. That is harmless (deterministic, and only happens at an overlap boundary) and is documented |
| Where reconciliation runs | On the **parsed rows in memory** in the worker, before insert | §6.4 checks the statement as the bank printed it. It does not need the stored order (an earlier draft of this plan said it did; that was wrong) |
| Reconciliation issues storage | `statement_uploads.reconciliation_issues` jsonb: `[{row_number, kind}]`, **no amounts** | `GET /uploads/{id}` must show which rows look wrong. Row numbers are enough to find them in the file |
| `merchant_key` on transactions | **Stored** (new nullable column) | User rules of type `merchant` and Phase 4 recurring detection both group by it. Phase 1 computed it and threw it away |
| `Channel` enum location | Moved to `app/parsers/base.py` (model imports it) | Same rule as `Direction`: the parser decides it, and the parser layer can't import models |
| Existing (Phase 1) rows | Not back-filled with channel/VPA/category. Migration back-fills `statement_seq` deterministically | Dev data only. Delete the upload and re-import to get the new fields. Documented |
| Scheduler | `apscheduler` (already a dependency) in `python -m app.workers.scheduler`, which only *enqueues* jobs | SPEC §11. Work happens in the worker, so a slow job never stalls the clock |
| `GET /uploads` | Newest first, **no pagination** | A user has tens of uploads, not thousands |
| Synthetic PDF fixtures | Generated into `tmp_path` by a builder (new dev dependency **`fpdf2`**, pure Python), like the `.xls` builder | No binary blobs in the repo; each variant is a few lines |
| User categories CRUD, `/me` endpoints, LLM | **Not in Phase 2** (Phase 3 / 4) | Rules can target system categories; nothing in Phase 2 needs the rest |

## Steps

Each step uses one branch `phase-2/step-<N>-<slug>` and ends with `ruff check`,
`ruff format --check` and `pytest`. It also ends with an explanation of every
file, a "What you should understand now" summary, a `docs/progress.md` update,
the list of files to stage, and a proposed commit. Then I stop for review.

### Step 0: Save the plan (`docs/phase-2-plan`)
- `docs/plans/phase-2.md` (this file), `docs/progress.md` (Phase 2 in progress)

### Step 1: Upload lifecycle schema + file encryption (`phase-2/step-1-upload-blobs`)
- `backend/pyproject.toml`: `fail_under = 100`
- Migration `0006`: `upload_blobs` (`upload_id` PK/FK CASCADE, `encrypted_content`
  bytea, `nonce` bytea, `created_at`). Drop `uq_statement_uploads_user_id_file_sha256`
  and create the partial unique index `WHERE status <> 'failed'`
- `backend/app/models/upload_blob.py`, update `statement_upload.py` (docstring + index)
- `backend/app/core/crypto.py`: `encrypt_file(content, upload_id)` /
  `decrypt_file(...)` with `cryptography`'s `AESGCM`, key from
  `settings.file_encryption_key_bytes`, random 12-byte nonce, **upload id as
  associated data** (a blob can't be swapped onto another upload)
- Update `imports.py` so the duplicate check and the race handler use the new
  index name (`violates_constraint`)
- Tests: round-trip, tampered ciphertext / wrong upload id / wrong key all fail;
  migration test; a `failed` row doesn't block the same file
- ADR 011 *Upload lifecycle* (supersedes 008), ADR 012 *Upload blobs in Postgres,
  AES-256-GCM*

### Step 2: RQ worker + asynchronous uploads (`phase-2/step-2-worker`)
- `backend/Dockerfile` (uv, Python 3.12), `docker-compose.yml`: `worker` service
  (`rq worker`, env from `.env`, DB/Redis hosts = service names)
- `backend/app/workers/queue.py`: `get_queue()` FastAPI dependency (tests
  override it with `is_async=False`)
- `backend/app/workers/jobs.py`: `process_statement(upload_id)`, which opens its own
  session and is idempotent (`completed` → no-op, missing blob → `failed`).
  On final failure (`on_failure` callback) it sets `failed` with a friendly message
  and deletes the blob
- Split `backend/app/services/imports.py`:
  - `create_upload()` (API side): size → account ownership → sha256 → file type by
    signature → encrypt + store blob → row `pending` → commit → enqueue
  - `process_upload()` (worker side): `processing` → decrypt → `parse_statement` →
    account check → `store_transactions` → delete blob → `completed`, all in one
    transaction
- `backend/app/api/v1/uploads.py`: `POST /uploads` → 202. New `GET /uploads`,
  `GET /uploads/{id}`, `DELETE /uploads/{id}` (409 while `processing`;
  CASCADE removes its transactions). `backend/app/schemas/upload.py` grows status +
  `error_message`
- Tests: upload → job → transactions visible; job args are exactly `(upload_id,)`;
  parse failure → `failed`, same file re-uploads fine; Redis down → 503 + `failed`;
  re-running the job is a no-op; IDOR 404 on GET/DELETE; delete removes rows;
  captured logs hold no narration/amount. Existing upload tests are updated to the
  202 + poll flow

### Step 3: Cleanup job + scheduler (`phase-2/step-3-cleanup`)
- `backend/app/workers/jobs.py`: `cleanup()` deletes blobs older than 24h and
  marks their uploads `failed` ("expired, please upload again"). It marks
  `pending`/`processing` rows older than 1h `failed` (lost job), and deletes expired
  refresh tokens
- `backend/app/workers/scheduler.py`: apscheduler `BlockingScheduler`, enqueues
  `cleanup` hourly. `docker-compose.yml`: `scheduler` service
- Tests: each cleanup rule with rows aged by setting `created_at`; idempotent
  second run; scheduler registers the job (unit, no clock waiting)

### Step 4: Statement order + balance reconciliation (`phase-2/step-4-reconciliation`)
- `backend/app/parsers/reconcile.py` (pure): `to_chronological(rows)`
  (newest-first statements reversed by date, with the balance chain as tie-breaker
  when all dates are equal), `reconcile(statement) -> list[Issue(row_number, kind)]`.
  The kinds are a broken per-row link `prev ± amount ≠ balance` and
  opening + credits − debits ≠ closing. No balance column → no check
- Migration `0008`: `transactions.statement_seq` int. Back-fill existing rows with
  `row_number() OVER (PARTITION BY upload_id ORDER BY txn_date, id)`, then
  NOT NULL. The keyset index is rebuilt as
  `(user_id, txn_date DESC, statement_seq DESC, id DESC)`.
  `statement_uploads.reconciliation_issues` jsonb
- `store_transactions` writes `statement_seq`. The worker sets `needs_review` when
  issues exist (data kept). `GET /uploads/{id}` returns the issues
- `backend/app/utils/cursor.py` + transactions service: 3-part cursor
- Tests: reconcile unit cases (clean, one broken link, newest-first, no balance,
  closing mismatch); integration: a broken fixture → `needs_review` with the right
  row; same-day rows listed in statement order; paging across a same-day boundary
  has no gaps/repeats; old 2-part cursors → 422
- ADR 013 *Statement order and reconciliation* (amends ADR 009's cursor)

### Step 5: Channel, VPA and merchant key from real narrations (`phase-2/step-5-narrations`)
- **First**, a golden test pinning `normalize_description` output + fingerprints
  for representative narrations (guards constraint 1)
- **Then** inspect the real ICICI `.xls` narrations locally (never committed).
  Show Dhruv the prefix patterns with counts and confirm the rules before writing
  them (SPEC §6.2/§17: no guessing)
- Move `Channel` to `app/parsers/base.py`. New `backend/app/parsers/narration.py`
  (pure): `detect_channel`, `extract_vpa`, VPA-aware `merchant_key`
  (P2P VPAs no longer yield the bank handle "YBL"). `NormalizedRow` gains
  `channel`, `counterparty_vpa`
- Migration `0009`: `transactions.merchant_key` text nullable +
  `(user_id, merchant_key)` index. `store_transactions` writes channel, VPA,
  merchant_key
- Merchant seed extended with merchants found in the real narrations (businesses
  only, no personal data)
- Tests: one synthetic narration per real pattern; golden test unchanged
- ADR 014 *Narration parsing*

### Step 6: Categorizer chain (`phase-2/step-6-categorizer`)
- New pure package `backend/app/categorizer/`: `base.py` (`TxnFacts`, `Decision`
  (slug, source, confidence), `Handler` protocol, `build_chain`), `handlers.py`
  (user manual, user rules, merchant dictionary, keyword rules, P2P, default → no
  decision). Rules and the merchant dictionary are passed in as plain data, so the
  package needs no database. Purity test like the parsers'
- Keyword rules from the real narrations (interest, ATM/ATW, salary, NACH+EMI,
  charges). P2P: UPI + VPA that looks like a phone number or personal handle +
  no merchant match + no business keyword → Transfers to People (heuristic, 0.7)
- `backend/app/services/categorization.py`: loads the dictionary, the user's rules
  and the slug→id map, then categorizes an upload's rows with
  `category_source = none` (never `user_manual`). It also sets `merchant_id`.
  Called by `process_upload` in the same transaction
- Insights: null bucket renamed "Not categorized yet"
- Tests: every handler + chain order; integration: upload → categorized rows,
  manual category survives a re-run, the null bucket label. The real statement's
  auto-categorized share is reported in the step summary
- ADR 015 *Categorizer chain*

### Step 7: User rules + `apply_to_similar` (`phase-2/step-7-rules`)
- Migration `0010`: `category_rules` (SPEC §5; unique `(user_id, match_type, pattern)`;
  `category_id` CASCADE, `created_from_transaction_id` SET NULL)
- `backend/app/models/category_rule.py`, `schemas/rule.py`, `services/rules.py`,
  `api/v1/rules.py`: `GET/POST/PATCH/DELETE /rules` (category must be usable by
  the user)
- `PATCH /transactions/{id}` with `apply_to_similar=true`: builds a rule from the
  row (VPA for P2P, else merchant key, else 422 "nothing to match on") and
  enqueues `recategorize_by_rule(rule_id)`. That job re-categorizes matching
  past rows except `user_manual`, reusing the Step 6 service
- Tests: CRUD, IDOR 404 on every route, duplicate rule → 409, apply_to_similar
  end to end, manual rows untouched

### Step 8: Self-transfer detection (`phase-2/step-8-self-transfers`)
- `backend/app/categorizer/self_transfer.py` (pure): one-to-one matching of a
  debit in account A to a credit of the same amount in account B (same user)
  within 0–2 days. Nearest date wins; an ambiguous tie is left unmatched
- `post_import_analysis(user_id)` job, enqueued after a successful import: marks
  both rows `is_self_transfer = true`, category Self Transfer, source heuristic.
  It skips `user_manual` rows and is idempotent
- Tests: savings→credit-card bill payment, 3-day gap not matched, same account
  not matched, tie left alone, re-run changes nothing, summary excludes the pair
- ADR 016 *Self-transfer matching*

### Step 9: ICICI PDF parser (`phase-2/step-9-icici-pdf`). **Blocked until the PDF is in `samples/`**
- Inspect the real PDF's layout first (header, columns, page breaks,
  multi-line narrations) and confirm it with Dhruv
- `readers.py`: PDF → pdfplumber, table extraction first, word-position fallback
  (SPEC §6.2). Repeated page headers/footers dropped. `backend/app/parsers/icici_pdf.py`
- `backend/tests/fixtures/icici_pdf.py` builder (dev dependency `fpdf2`)
- Key test: the **same period as `.xls` and as PDF → the second import is 100%
  duplicates** (narrations must normalize identically across formats)
- ADR 017 *PDF parsing*

### Step 10: Password-protected PDFs (`phase-2/step-10-pdf-passwords`)
- Dependency `pypdf`. `create_upload`: encrypted PDF without password → row
  `needs_password`, bank-encrypted file kept in the (app-encrypted) blob, **409**
  with `upload_id` in `details`. With a password, or via
  `POST /uploads/{id}/password`: unlock in memory → store the unlocked copy →
  enqueue. Wrong password → 400, stays `needs_password`. That endpoint is
  rate-limited 5/min/user
- Tests (synthetic PDFs encrypted with pypdf): both flows; wrong password; job
  args + serialized Redis job + captured logs + DB row contain no password;
  expired `needs_password` blob cleaned by Step 3's job
- ADR 018 *Statement passwords*

### Step 11: Acceptance + wrap-up (`phase-2/step-11-acceptance`)
- `docs/phase-2-acceptance.md`: Part A automated against the Docker worker,
  Part B by hand with the real `.xls` + PDF
- README: running the worker/scheduler, the new upload flow. Progress + plan updated

**Order note:** Steps 9–10 come last only because the PDF isn't in `samples/` yet.
If it arrives earlier, Step 9 can move up after Step 5 (it needs Step 2's worker
and Step 5's narration rules).

## Verification (per step and at the end)
- `cd backend && uv run ruff check . && uv run ruff format --check . && uv run pytest --cov`
  (with `docker compose up -d postgres redis`). Never run two pytest runs at once
  (shared test DB, see progress Known issues)
- Integration tests run every job synchronously through the overridden queue
- From Step 2 on, a live check: `docker compose up -d --build worker`, upload
  through `/docs`, poll `GET /uploads/{id}`, read the worker logs for leaks
- Step 11 runs the full acceptance checklist against the real files

## Change log
- **Step 1 (done):** the code review of the PR (5 findings, all fixed on the branch) added
  **migration 0007**, so the later steps' migrations move up by one (Step 4 → 0008,
  Step 5 → 0009, Step 7 → 0010). The partial unique index of 0006 had replaced the only
  index starting with `user_id`, which left the CASCADE from `users` and the Step 2 upload
  list on sequential scans; 0007 adds `(user_id, created_at DESC)`. Migration 0006 was
  already committed, so the index is a new migration (rule 7). Also from the review:
  - the "not failed" condition is one literal-SQL constant shared by the index and the
    duplicate check, because a bound `'failed'` stops Postgres from using the partial
    index in a prepared plan (measured, and tested);
  - `decrypt_file` reports a damaged nonce as `FileDecryptionError`;
  - stale `unique(user_id, file_sha256)` references were updated, and ADR 006 got an
    amendment note.
- **Step 2:** three changes to the plan as written.
  - **Migration 0008** (`statement_uploads.column_mapping` jsonb, nullable). A generic
    CSV's mapping has to reach the worker, and a job's only argument is the upload id,
    so it is stored on the row (decided with Dhruv: kept on the upload row, not on
    `upload_blobs`, not in the job). The later migrations move up again: Step 4 → 0009,
    Step 5 → 0010, Step 7 → 0011.
  - **ADR 013** *Uploads are imported by an RQ worker* records the step's decisions, so
    the planned ADRs move up by one: Step 4 → 014, Step 5 → 015, Step 6 → 016,
    Step 8 → 017, Step 9 → 018, Step 10 → 019.
  - **`on_failure` runs after every failed attempt**, not only the last one, in RQ 2.12
    (found in RQ's source). The callback checks `job.should_retry` and only marks the
    upload `failed` when no retry is coming. The worker is started with
    `python -m app.workers.worker` rather than `rq worker`, so that our log redaction
    is configured in the worker process (ADR 013). PDFs are refused in the request
    (400) until Step 9, since the signature already says what they are.
- **Step 3:** "deletes expired refresh tokens" became "deletes a token family once every
  token in it has expired". Deleting single expired tokens would let a thief who outlasts
  the stolen token escape reuse detection (ADR 005 amendment). No new migration or ADR,
  so the numbering from Step 2's entry stands. The stale threshold is shared with Step 2
  (`STALE_UPLOAD_AFTER`, 1 h).
- **Step 3 review:** **migration 0009** (index on `refresh_tokens.expires_at`) was added, so
  the later migrations move up again: Step 4 → 0010, Step 5 → 0011, Step 7 → 0012. ADR
  numbers are unchanged (the decisions went into ADR 005 and ADR 013 amendments). Also
  from the review: "stale" now also requires that no job of the upload is waiting in Redis
  (fixed job id `process_statement_{upload_id}`).
- **Step 4:** done as planned, as migration **0010** and **ADR 014** (the numbering
  above). Two details the plan left open, decided in the step:
  `reconciliation_issues` is NULL for "not checked" and [] for "checked, clean", and at
  most 100 issues are stored (the full count is in `reconciliation_errors`). The plan's
  "3-part cursor" means `(txn_date, statement_seq, id)`; old 2-part cursors get a 422.
