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
| Full test suite (`pytest`) | **all passing** (848 at the time of the run, and growing) |
| Coverage | **100%** of statements and branches (99% at the time of the run; closed 2026-10-13) |
| Live-server end-to-end flow, synthetic ICICI `.xls` | **73/73 checks** |

**Coverage was 99% at the acceptance run. The gap is now closed.** Every
uncovered line was a `__repr__`, a bare `raise` on an error path we
re-raise, or a fallback `return ""` / `return None` — nothing that decided
a money value, a `user_id` filter or a status code. They are now tested
rather than excused: `tests/unit/test_model_reprs.py` is new, and
`test_rate_limit_keys.py`, `test_readers.py`, `test_icici.py`,
`test_auth.py`, `test_accounts.py`, `test_uploads.py` and `test_seed.py`
gained the missing cases (873 tests, **100% of statements and branches**).

The single deliberate exclusion is the `if __name__ == "__main__":` guard
in `app/db/seed.py`: a script entry point cannot run inside the test
process, and the `main()` it calls is itself tested. It is declared in
`[tool.coverage.report]` in `backend/pyproject.toml`, so the exclusion is
one visible line rather than a scattering of `# pragma: no cover`.

No per-module table is written here, for the reason this document already
learned once: **`pytest --cov --cov-report=term-missing` is the live source
of truth**, and a number copied into prose is a claim that rots. What
matters for acceptance is the shape of the gap, not its size — and nothing
on a money, ownership or status-code path is untested.

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

## Part B — the real statement

> **PASSED, 2026-10-13.** Run against the real ICICI `.xls` for August 2026
> (175 transaction lines, account ending 5606). Results below, in
> [Outcome](#outcome). The checklist is kept as written so Phase 2 can
> re-run it after the worker and the PDF parser land.

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
| Run on | 2026-10-13 |
| Statement | ICICI savings, account ending 5606, 2026-08-01 → 2026-08-31, 175 transaction lines |
| Result | **PASS** |

### What was checked, and how

| Step | Result |
|---|---|
| B2 account | `account_last4` read as **5606**; confirmed as the real account by the balance match below |
| B3 upload | `completed`, period `2026-08-01 … 2026-08-31`, **175 parsed / 175 inserted / 0 duplicate** |
| B4 balance chain | **holds for all 174 links** |
| B4 identity | ₹1,11,541.33 − ₹99,036.54 + ₹74,414.50 = ₹86,919.29 = closing, exactly |
| B4 netbanking | opening and closing **confirmed by Dhruv** against netbanking |
| B5 re-upload | identical file → **409 CONFLICT**; a second user *can* upload it (sha is unique per user, not globally) |
| B6 categorize | `category_source: user_manual`; the row moved category and the total was unchanged; a `transfer` category set `is_self_transfer` and spend dropped by exactly that row |
| B7 summary | spend ₹99,036.54 and income ₹74,414.50 **equal the file's own totals**; count 175; `previous` = `2026-07` with `null` change percentages |
| B7 breakdown | `total_spend_paise` equals spend; the parts add up; one `category_id: null` bucket at 100% before categorizing |

**The decisive check was the balance chain, not the row count.** The
statement carries the bank's running balance, which the importer stores but
never uses to *derive* anything, so for consecutive rows in file order:

    balance[n] - balance[n-1]  ==  +amount (credit)  or  -amount (debit)

That held for all 174 links. A misread amount, a flipped direction, or a
dropped or duplicated interior row would each have broken it — so every
amount and every direction we parsed agrees with independent data inside
the file. What the chain *cannot* catch is rows dropped from the very start
or end of the table, which is why the opening and closing balances were
confirmed against netbanking separately.

### Two things the run did not cover

- **The overlapping-statement case** (the second half of B5) needs a
  second export covering an overlapping period; only one file exists. The
  behaviour is covered by
  `test_an_overlapping_statement_inserts_only_new_rows`, but not against
  real data.
- **Dates** are not verified by the chain. They were ascending across all
  175 rows, with 30 distinct dates in a 31-day month, which is strong
  circumstantial evidence — but a date misread *within* the month would
  survive every check here.

### A false alarm worth remembering

The chain check first reported **151 of 174 links broken**. That was the
checker's bug, not the importer's: it ordered rows by `(txn_date, id)` as
`GET /transactions` returns them, and ids are UUIDv4 with nothing storing
the statement's row order — so with ~6 rows a day, most consecutive pairs
were shuffled within a day. Re-run in the parser's own file order, it was
174/174.

That is the "same-day ordering" item in `docs/progress.md`, and this
upgrades it from cosmetic to blocking: **the balance column is unusable for
reconciliation until the statement's row sequence is stored**, which makes
it a prerequisite for SPEC §6.4 balance reconciliation in Phase 2, not an
independent nicety.

---

Anything that does not match on a future run: give **counts, dates and the
mismatching field**, never the statement or a narration from it. Real bank
data stays in `samples/`, which is gitignored (CLAUDE.md rule 8).
