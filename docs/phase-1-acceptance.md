# Phase 1 — acceptance run

The "Done when" list in [plans/phase-1.md](plans/phase-1.md) checked off
against a **real ICICI statement**, by hand, through `/docs`.

Two halves, because they prove different things:

- **Part A** is automated and already green — it runs against a synthetic
  statement, so it can live in CI and be re-run forever.
- **Part B** is the one that needs a person and a real file. Nothing
  synthetic can tell you whether the numbers match the statement the bank
  actually sent, because the fixture was written from our own reading of
  that layout. If the parser misunderstood a column, the fixture
  misunderstands it the same way and both agree, wrongly. Only the paper
  statement breaks that tie.

---

## Part A — automated (done)

| Check | Result |
|---|---|
| `ruff check` / `ruff format --check` | clean |
| Full test suite (`pytest`) | **848 passed** |
| Coverage | 99% overall, branch coverage on; 100% of the five modules Step 9 added |
| Live-server end-to-end flow, synthetic ICICI `.xls` | **73/73 checks** |

**Coverage is 99%, not 100%, and the gap is in Phase 1 code.** Nine modules
added during this phase still have uncovered lines:

| Module | Step | Cover |
|---|---|---|
| `models/user.py`, `models/refresh_token.py` | 1 | 93% |
| `services/auth.py` | 2 | 94% |
| `core/rate_limit.py` | 3 | 94% |
| `models/account.py` | 4 | 96% |
| `services/accounts.py` | 4 | 97% |
| `parsers/readers.py` | 6 | 97% |
| `parsers/icici.py` | 6 | 99% |
| `services/imports.py` | 7 | 98% |

Inspected line by line, every uncovered line is a `__repr__`, a bare
`raise` on an error path we re-raise, or a fallback `return ""` / `return
None`. Nothing uncovered decides a money value, a `user_id` filter or a
status code. That is reassuring but it is still not 100%, so it stays a
Phase 2 cleanup item rather than something to round up here.

The live run was driven against real `uvicorn` on `127.0.0.1:8111` with
`DATABASE_URL` pointed at `kharcha_test`. It covered register → login
(including a wrong password → generic 401) → create account → upload →
re-upload (409) → list → page by cursor → categorize → both insights
endpoints → self-transfer exclusion → cross-user isolation → validation
errors → security headers.

**What that adds over the test suite is almost nothing, and it is worth
being precise about why.** The tempting claim is that a live server
exercises things `TestClient` cannot — ASGI startup, middleware order,
multipart parsing, the Redis rate limiter, the OpenAPI schema. Checked one
by one, the committed tests already cover every one of them:

| Supposedly unique to a live server | Already covered by |
|---|---|
| ASGI lifespan startup/shutdown | `test_app.py::test_app_starts_and_shuts_down_cleanly` (uses `with TestClient(...)`) |
| Middleware order | `test_rate_limit.py::test_429_passes_through_the_other_middleware`, `test_app.py::test_cors_preflight_also_gets_security_headers` |
| The real Redis rate limiter | `test_rate_limit.py` (real counters, plus a Redis-down case) |
| Real multipart parsing | `test_uploads.py` (real multipart bodies, real Starlette parser) |
| The OpenAPI schema | `test_app.py::test_health_is_listed_in_openapi_docs` |

The only thing genuinely exclusive to Part A is the **HTTP transport**:
uvicorn's own HTTP/1.1 parsing over a real socket, where `TestClient` calls
the ASGI app in process. That is a thin layer and not ours.

So Part A is an **independent end-to-end confirmation**, not extra
coverage. Its value is that it was written separately from the test suite
and agreed with it — a decent check that the suite is not passing because
of how it sets itself up. `tests/integration/` remains the thing CI runs
and the thing to keep.

It also confirmed **rule 3** against real server logs: no narration,
amount, passphrase or token appeared in any of the 46 log lines — only
method, path, status, duration and request id.

> Re-running Part A: the script lives outside the repo (it was a one-off
> verification, not a committed test). Everything it asserts is also
> asserted by `tests/integration/`, which is what CI runs. Say the word if
> it should become a committed `scripts/smoke.py` for Phase 2.

---

## Part B — the real statement (for Dhruv)

**Before you start.** Use your development database, not the test one —
the test database is wiped by every `pytest` run.

```bash
docker compose up -d
cd backend
uv run alembic upgrade head
uv run python -m app.db.seed
uv run uvicorn app.main:app --reload
```

Then open <http://127.0.0.1:8000/docs>. The steps below match the
[README walkthrough](../README.md#walkthrough-phase-1); fill in the right
column as you go.

### B1. Register and log in

| Step | Expect | Actual |
|---|---|---|
| `POST /auth/register` | `201`, body has your user, **no** token | |
| `POST /auth/login` | `200`, `access_token` + `expires_in: 900` | |
| Click **Authorize**, paste the token | padlocks close | |

### B2. Add the real account

Read the last 4 digits off the statement itself, not from memory — the
import compares them and refuses a mismatch.

| Step | Expect | Actual |
|---|---|---|
| `POST /accounts` with `bank_code: ICICI`, the real `masked_number` | `201` | |
| Note the `id` | — | `account_id =` |

### B3. Upload the real `.xls`

| Step | Expect | Actual |
|---|---|---|
| `POST /uploads` — file + `account_id`, `column_mapping` empty | `201`, `status: completed` | |
| `period_start` / `period_end` | the statement's own period | |
| `rows_parsed` | **= the number of transaction lines you count on the statement** | |
| `rows_inserted` | same as `rows_parsed` | |
| `rows_duplicate` | `0` | |

> If `rows_parsed` is **one or two short**, suspect a narration that spilled
> onto a continuation row that was *not* joined, or a last row swallowed by
> the end-of-table guard. If it is **higher**, a footer or legend line is
> being read as a transaction. Either way, tell me the count and the
> period and I will work from that — do not send me the file.

### B4. Compare against the paper statement

This is the part that actually matters. `GET /transactions` with
`from`/`to` set to the statement period and `limit=200`.

| Check | Expect | Actual |
|---|---|---|
| First row (newest) | matches the statement's last line: date, amount, narration | |
| Last row (oldest) | matches the statement's first line | |
| Count of `direction: debit` | = withdrawals on the statement | |
| Count of `direction: credit` | = deposits on the statement | |
| Sum of debit `amount_paise` | = total withdrawals × 100 | |
| Sum of credit `amount_paise` | = total deposits × 100 | |
| **opening − debits + credits** | **= closing balance** | |
| A narration the bank split over two lines | arrives as one unbroken string | |
| Any amount with paise (e.g. ₹1,250.50) | `amount_paise` ends in `50`, `amount_display` shows `.50` | |

The balance identity is the strongest single check: it fails if even one
amount is misread, a row is missed, or a direction is flipped.

### B5. Re-upload, and overlap

| Step | Expect | Actual |
|---|---|---|
| Upload the **same file** again | `409 CONFLICT` | |
| Upload an **overlapping** period, if you can export one | `201`, the shared rows in `rows_duplicate`, only new ones in `rows_inserted` | |
| `GET /transactions` total after that | no duplicated rows | |

### B6. Categorize by hand

| Step | Expect | Actual |
|---|---|---|
| `GET /categories` | 25 system categories, all `is_system: true` | |
| `PATCH /transactions/{id}` with a `category_id` and a note | `200`, `category_source: user_manual` | |
| Same row with a `transfer`-kind category (*Self Transfer*) | `is_self_transfer: true` | |
| `GET /transactions?category_id=<that one>` | the row appears | |

### B7. Insights

| Step | Expect | Actual |
|---|---|---|
| `GET /insights/summary?month=<the statement's month>` | `income`/`spend`/`net` match your B4 sums — **minus anything you flagged as a self transfer in B6** | |
| `net_display` | Indian grouping, e.g. `₹48,179.50` | |
| `previous` block | the month before; `*_change_pct` is `null` if you have no data there | |
| `GET /insights/categories?from&to` over the period | `total_spend_paise` = your debit sum minus self transfers | |
| The items | add up to `total_spend_paise`; mostly the `null` "Uncategorized" bucket | |

> Remember when reconciling B7 against B4: a row you filed under *Self
> Transfer* in B6 is **deliberately** excluded from both insights
> endpoints, so the summary's spend will be lower than B4's raw debit sum
> by exactly that amount. That is the feature, not a discrepancy.

---

## Outcome

| | |
|---|---|
| Run on | _(date)_ |
| Statement | _(bank, period, row count — never the file itself)_ |
| Result | _(pass, or what differed)_ |

Anything that does not match: give me the **counts, dates and the
mismatching field**, never the statement or a narration from it. Real bank
data stays in `samples/`, which is gitignored (CLAUDE.md rule 8).
