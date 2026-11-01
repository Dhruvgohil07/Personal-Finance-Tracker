# Phase 3 — Frontend MVP

Approved by Dhruv on 2026-11-02.

## Goal
A Next.js web app in plain JavaScript, on top of the Phase 2 API. A user can
sign up, add accounts, upload statements, browse and correct transactions, and
see the month at a glance. Backend and frontend are deployed, so someone other
than the developer can use it.

SPEC §16: *"Next.js (plain JavaScript) app: auth pages, onboarding, upload page
with status polling, transactions table with filters and category editing, basic
dashboard. Deploy backend + frontend."*

### Where Phase 2 left off
- Every feature is reachable only through `/docs` (Swagger UI).
- There is no `/me`, and no way to delete an account.
- Rate limits key on the socket IP. Behind any proxy, every client shares one
  (progress, Known issues).
- Nothing is deployed. The RQ worker and the scheduler run as Docker services
  locally.

## Done when
- **A family member can sign up, upload a statement, and understand their
  monthly spending without help** (SPEC §16). Checked with a real person in
  Step 12.
- Backend: `ruff`, `pytest` at **100% coverage (enforced)**, `backend` CI green.
- Frontend: ESLint, Vitest and `next build` pass in a new `frontend` CI job.
- The app runs at public URLs (Vercel + Render).
- Explicitly **not** done: `PATCH /me/settings` and the AI toggle, CSV export,
  custom categories, trend and top-merchant charts (Phase 4); subscriptions,
  budgets and alerts pages (Phases 4–5); a faster `q=` search (still deferred).

## Hard constraints
1. **The access token is never persisted in the browser** (no `localStorage`,
   `sessionStorage` or readable cookie). The refresh token stays an httpOnly,
   Secure, `SameSite=Strict` cookie (ADR 005).
2. **The browser only ever talks to the frontend's own origin.** The API is
   reached through the Next.js proxy, so the cookie never has to cross sites and
   no CORS is needed.
3. **No component is left unfinished** (CLAUDE.md §0, SPEC §12). Every React
   concept is explained as it first appears: state, effects, props, data
   fetching.
4. **Money in the browser is formatted, never computed with floats.** Amounts
   arrive as integer paise and are only formatted (`Intl.NumberFormat('en-IN')`,
   lakh grouping). Totals come from the API.
5. **Every backend rule still holds**: user scoping and IDOR tests, no sensitive
   logs, passwords never persisted, migrations append-only (CLAUDE.md §4).

## Decisions made during planning

### Settled with Dhruv
| Question | Answer |
|---|---|
| Backend work besides the frontend | **`GET /me` + `DELETE /me`**. CSV export and custom categories stay in Phase 4 (as SPEC §16 says, correcting what progress had queued). `q=` search stays as it is; it is fine at personal-finance size |
| How the browser reaches the API | **Next.js proxy**: `/api/v1/*` is rewritten to the backend. Rejected: calling the API's own origin with CORS, because the refresh cookie would then have to become `SameSite=None`, weakening the CSRF protection ADR 005 relies on |
| Frontend testing | **ESLint + Vitest + React Testing Library**, run by a new `frontend` CI job. Rejected: Playwright end-to-end (heavier CI for little more at this size); lint only (the token-refresh logic needs real tests) |
| Deployment | **One free container**: Vercel (frontend), a Render web service running API + worker + scheduler, Neon (Postgres), Upstash (Redis). Rejected: a separate paid worker service (SPEC §15's shape, but not free); deferring deployment (the Done-when needs another person) |

### Design decisions (proposed; each confirmed when its step starts)
| Decision | Choice | Why |
|---|---|---|
| Where the access token lives | **In memory only**. On page load, one silent `POST /auth/refresh` restores the session from the cookie | A script injected into the page (XSS) cannot read a token that was never stored |
| Refreshing | **One refresh at a time**: requests that get a 401 together wait for the same refresh, then retry once | Refresh tokens rotate; a second refresh with the already-used token counts as reuse and revokes the whole family (ADR 005), which would log the user out |
| Client IP behind the proxy | Next.js `middleware.js` forwards the browser's IP in a header, **signed with a shared secret** (`PROXY_SHARED_SECRET`); the backend trusts it only when the secret matches, otherwise it uses the socket IP | Behind the proxy every visitor shares one IP, so 5 failed logins a minute from anyone would lock out everyone. `X-Forwarded-For` cannot be trusted, because Vercel's IPs are not a fixed list |
| `DELETE /me` | Requires the **current password**; cascades through every table; clears the cookie; writes an `audit_log` row whose `user_id` has **no foreign key**, so the record survives the deletion. No email or financial values in it | CLAUDE.md §7.3; asking for the password again means a stolen session cannot delete the account |
| Three processes in one container | A small launcher, `app/run_all.py` (no new dependency), starts uvicorn, the RQ worker and the scheduler, and **exits if any of them exits**, so Render restarts the whole container | A crashed worker would otherwise go unnoticed while the API looks healthy |
| "This is a guess" badge | Shown for `heuristic` rows (confidence ≤ 0.80) | SPEC §12's low-confidence badge, from the values that exist today |
| Frontend coverage | Thorough tests for the logic (API client, money formatting, the CSV mapping form), smoke tests for pages, **no enforced percentage** | 100% of rendering code would test markup, not behaviour |
| Upload status polling | TanStack Query `refetchInterval`, from the `Retry-After` header, stopping at a final status | The API already says how often to poll (Phase 2 Step 2) |

### Frontend dependencies (approved with the testing choice)
- Runtime (SPEC §2): `next`, `react`, `react-dom`, `tailwindcss`,
  `@tanstack/react-query`, `recharts`.
- Dev only: `eslint` with Next's config, `vitest`, `@testing-library/react`,
  `@testing-library/jest-dom`, `jsdom`, `@vitejs/plugin-react`. The last two are
  what Vitest needs to render React components.
- Exact versions are pinned in Step 3; anything else is asked about first.

## Steps

Each step uses one branch `phase-3/step-<N>-<slug>`. Every step ends with
`ruff check`, `ruff format --check` and `pytest`; from Step 3 on, also
`npm run lint`, `npm test` and `npm run build`. It also ends with an
explanation of every file, a "What you should understand now" summary, a
`docs/progress.md` update, the list of files to stage, and a proposed commit.
Then I stop for review.

### Step 0: Save the plan (`docs/phase-3-plan`)
- `docs/plans/phase-3.md` (this file), `docs/progress.md` (Phase 3 in progress)

### Step 1: `GET /me` + `DELETE /me` (`phase-3/step-1-me`)
- `backend/app/models/audit_log.py`, migration **0013** (`audit_log`: `id`,
  `user_id` uuid **without FK**, `action`, `entity`, `entity_id`, `ip`,
  `user_agent`, `created_at`; SPEC §5)
- `backend/app/schemas/me.py`, `backend/app/services/me.py`,
  `backend/app/api/v1/me.py`
- `GET /me`: id, email, name, settings. `DELETE /me` with `{"password": ...}`:
  wrong password → the generic 401; then cascade, revoke the refresh tokens,
  clear the cookie, write the audit row, 204
- Tests: only your own profile; after a delete, zero rows of yours in **every**
  table while another user's data is untouched; the audit row is written with
  no email; the old refresh token is rejected; a wrong password changes nothing
- ADR 021 *Account deletion*

### Step 2: Client IP behind the proxy (`phase-3/step-2-client-ip`)
- `backend/app/core/rate_limit.py` (`client_ip`), `backend/app/core/config.py`
  (`PROXY_SHARED_SECRET`, optional), `.env.example`
- Tests: the header without the secret, or with a wrong one, is ignored; with
  it, two browsers behind one proxy get separate login limits
- ADR 007 amendment; closes the "behind a proxy" known issue

### Step 3: Frontend scaffold (`phase-3/step-3-frontend-scaffold`)
- `frontend/` via `create-next-app` (JavaScript, App Router, Tailwind, ESLint)
- `next.config.mjs`: rewrites `/api/v1/:path*` to `API_ORIGIN`;
  `middleware.js`: the signed client-IP header
- `src/lib/api.js` (fetch wrapper: bearer token, the error shape, one refresh
  at a time, retry once); `src/lib/money.js` (paise → `₹1,25,000.00`)
- The auth context, the TanStack Query provider, the base layout, dark mode
- `.github/workflows/ci.yml`: new `frontend` job (`npm ci`, lint, test, build);
  `.gitignore`: `node_modules`, `.next`, `.env.local`
- Tests: a 401 → refresh → retry; three simultaneous 401s cause one refresh; a
  failed refresh clears the session; money formatting incl. lakh grouping and
  negatives
- ADR 022 *Frontend session and API proxy*
- After merge, Dhruv adds `frontend` to the `protect-main` required checks

### Step 4: Auth pages (`phase-3/step-4-auth-pages`)
- Register, login, logout; a guard that sends logged-out visitors to `/login`;
  a header showing the user's name (`GET /me`)
- Error states: the generic login error, validation errors per field, 429 with
  the wait time

### Step 5: Accounts + onboarding (`phase-3/step-5-accounts-onboarding`)
- Accounts page: list, add, rename, delete (with the "this removes its
  transactions" warning)
- First run: add an account → upload the first statement → see the results.
  Goal: value in under 2 minutes (SPEC §12)

### Step 6: Uploads (`phase-3/step-6-uploads`)
- Upload form (file, account, optional password); status polling; history with
  counts, `needs_review` row numbers, `failed` messages; delete, with the
  meaning of its 409s
- The password prompt: 409 `PASSWORD_REQUIRED` → ask → `POST /uploads/{id}/password`;
  a wrong password lets you try again

### Step 7: CSV column mapping (`phase-3/step-7-csv-mapping`)
- For a bank without its own parser: the browser reads the CSV's header row and
  offers dropdowns (date, description, debit/credit or amount, balance) plus a
  date-format choice, then uploads with `column_mapping`
- Tests: header reading (BOM, `;` delimiter, quoted labels), the
  two-columns-or-one-amount rule, the request it builds

### Step 8: Transactions (`phase-3/step-8-transactions`)
- Filters (date range, account, category, direction, search, amount range) kept
  in the URL; "Load more" by cursor
- Inline category edit with the **"apply to similar?"** prompt; notes; the guess
  and self-transfer badges
- Cards on a phone, a table on a desktop; empty, loading and error states

### Step 9: Dashboard (`phase-3/step-9-dashboard`)
- This month's income, spend and net against last month (`/insights/summary`);
  a category donut (Recharts, `/insights/categories`); a month picker
- "N transactions need your attention", linking to the uncategorized ones

### Step 10: Settings (`phase-3/step-10-settings`)
- Profile (from `GET /me`); **delete account** behind a typed confirmation and
  the password; afterwards, back to the start page with the session gone

### Step 11: Deployment (`phase-3/step-11-deploy`)
- `backend/Dockerfile` for the single container, `backend/app/run_all.py`;
  `render.yaml` (release step: `alembic upgrade head` + seed; health check
  `/api/v1/health`); Vercel settings (`API_ORIGIN`, `PROXY_SHARED_SECRET`)
- README: the full deployment guide (SPEC §15)
- Dhruv creates the accounts and enters the secrets; I verify the deployed URLs
- First check: **the RQ worker polls Redis continuously**, which may run into
  Upstash's free command limits. If so, Render's own Redis is the fallback
- ADR 023 *Deployment topology*

### Step 12: Acceptance + wrap-up (`phase-3/step-12-acceptance`)
- `docs/phase-3-acceptance.md`:
  - Part A: the automated checks, plus a smoke run against the deployed URLs;
  - Part B: **a family member uses the app while Dhruv watches**, and every
    place they got stuck is recorded.
- README, progress and plan updated

## Verification (per step and at the end)
- Backend: `cd backend && uv run ruff check . && uv run ruff format --check . &&
  uv run pytest --cov` (with `docker compose up -d postgres redis`). Never two
  pytest runs at once (shared test database, progress Known issues)
- Frontend (from Step 3): `cd frontend && npm run lint && npm test && npm run build`
- From Step 4, a live check: API, worker and `npm run dev` together, driven in a
  browser, with the worker's logs read for leaks
- Step 12 runs the full acceptance checklist against the deployed app

## Change log
- (none yet)
