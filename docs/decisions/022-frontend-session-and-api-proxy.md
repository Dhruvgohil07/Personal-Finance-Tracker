# ADR 022 — Frontend session and API proxy

- Status: accepted
- Date: 2026-11-02 (Phase 3, Step 3)

## Context
Phase 3 adds a Next.js web app (plain JavaScript) on top of the API. The backend's
auth design is fixed by ADR 005: a 15-minute access JWT returned in the response
body, and a rotating refresh token in an httpOnly, Secure, `SameSite=Strict` cookie
whose reuse revokes the whole session. The Phase 3 plan adds two hard constraints:
the access token is never persisted in the browser, and the browser only talks to
the frontend's own origin. This ADR records how the frontend meets them.

## Decision

**1. The browser talks to one origin; Next.js forwards API calls.**
`next.config.mjs` rewrites `/api/v1/:path*` to `API_ORIGIN` (the backend). The
browser asks `https://app/api/v1/accounts`; the Next.js server fetches the backend
and passes the answer back, `Set-Cookie` included. So the refresh cookie is
first-party and stays `SameSite=Strict`, and the backend needs no CORS for the app.
A Vercel build without `API_ORIGIN` fails instead of forwarding to localhost.

**2. The proxy signs the browser's IP** (the backend side is ADR 007's Phase 3
amendment). `src/proxy.js` (Next.js 16's new name for Middleware) runs before the
rewrite, only for `/api/v1/*`, and passes `NextResponse.next({ request: { headers } })`
so the headers reach the rewrite's destination. It always removes any
`x-kharcha-client-ip*` headers the browser sent, then, when `PROXY_SHARED_SECRET`
is set and the IP is known, adds the IP and its HMAC-SHA256 (Web Crypto API).
- The IP is the first entry of `x-forwarded-for`, chosen with Dhruv over the
  `@vercel/functions` package (no new dependency). It is trustworthy only because
  **Vercel overwrites** that header with the address it received the connection
  from. On a host that merely appends to it, a client could put any IP first.
- Locally there is no such header, so nothing is signed and the backend uses the
  connection's address.
- Checked end to end with both servers running: two browsers got separate login
  limits, a forged header was removed, and `Set-Cookie` came back with all its
  attributes.

**3. The access token lives in memory only.** It is a variable inside the API
client (`src/lib/api.js`): never in `localStorage`, `sessionStorage` or a readable
cookie, so a script injected into the page (XSS) has nothing stored to steal. A
page load restores the session with one silent `POST /auth/refresh`, which the
browser authenticates with the httpOnly cookie. `src/lib/auth.jsx` mirrors the
token into React (`useSyncExternalStore`) and exposes `{ status, login, logout }`.

**4. One refresh at a time.** Refresh tokens rotate, and presenting a used one
revokes the session (ADR 005). When several requests get a 401 together, they all
await the **same** refresh promise, and each is retried once. A 401 that arrives
after another call already refreshed is retried with the new token without a
second refresh. Only a 401 from `/auth/refresh` ends the session; a 500 or a
network error there keeps it (logging out for a server hiccup would be wrong).
The `/auth/*` routes are never refreshed-and-retried: their 401 means wrong
credentials.

**5. Ending a session clears TanStack Query's cache**, so the next person at the
browser never sees the previous user's data.

**6. Money is only formatted.** `formatInr` turns integer paise into `₹1,25,000.00`
with integer arithmetic and `Intl.NumberFormat("en-IN")`, giving the same text as
the backend's `format_inr` (shared test cases, including 2^53 − 1).

## Alternatives rejected
- **Calling the API's own origin with CORS.** The refresh cookie would become
  third-party and need `SameSite=None`, weakening the CSRF protection ADR 005
  relies on.
- **Keeping the access token in `localStorage`.** It survives reloads, but any
  XSS can read it.
- **A Next.js API route per backend endpoint (backend-for-frontend).** Re-implements
  every endpoint; a rewrite forwards them all.
- **Letting each 401 refresh on its own.** Parallel refreshes with one cookie look
  like token theft and log the user out.

## Consequences
- Every API call passes through the Next.js server: one extra hop.
- `API_ORIGIN` and `PROXY_SHARED_SECRET` must be set on Vercel (Step 11); the secret
  must equal the backend's.
- The `x-forwarded-for` trust holds on Vercel only; moving the frontend to another
  host means revisiting decision 2.
- ESLint is 9.39.5, not 10: three plugins in `eslint-config-next` (react, import,
  jsx-a11y) do not support ESLint 10 yet. npm marks ESLint 9 as unsupported, and
  `npm audit` reports a moderate `minimatch` denial-of-service in that toolchain.
  It runs only on developer machines and in CI, never on user input; revisit when
  `eslint-config-next` supports ESLint 10.

## Review of PR 30 (2026-11-03)
Three fixes, each checked so that its test fails without it:

1. **Request bodies over 10 MB.** Because `src/proxy.js` exists, Next.js copies
   every request body into memory and cuts it off at `proxyClientMaxBodySize`
   (10 MB by default). A statement upload (up to the backend's 10 MB
   `MAX_UPLOAD_MB`, plus the multipart framing) failed with a 500 before reaching
   the backend. Narrowing the proxy's `matcher` does NOT help: checked live, an
   excluded path was still buffered and cut off. The fix is
   `experimental.proxyClientMaxBodySize: "12mb"` in `next.config.mjs`. Checked live
   against a stand-in backend that counts bytes: an 11 MB body now arrives whole;
   13 MB still fails with Next's 500.
   - Consequence for Step 6: the upload page must refuse files over the backend's
     limit **in the browser**, before sending, so the user never meets that 500.
     Raise both limits together.
   - Consequence for Step 11: the hosting platform may have its own request size
     limit for a proxy; verify an upload near 10 MB on the deployed app.
2. **Refreshing in several tabs at once.** The one-refresh-at-a-time guard was per
   tab, so tabs refreshing together sent the same cookie twice and the backend
   refused the second, logging that tab out. The refresh now runs inside a Web Lock
   (`navigator.locks`, name `kharcha-refresh`): every tab of the site waits for the
   others, then refreshes with the cookie the previous tab received. Where the API
   is missing (old browsers, insecure contexts), it falls back to the per-tab guard.
3. **A refresh that lands after logout.** A session generation counter changes on
   every login, logout and refresh. A refresh whose answer arrives after the
   generation changed is ignored, so a late answer can't sign a logged-out user back
   in (its cookie is dead anyway: logout revoked the whole family).

## Phase 3 Step 4: the auth pages (2026-11-03)
- **The guard runs in the browser only** (`src/components/require-auth.jsx`). The
  server can't know whether someone is signed in: the token is in memory, and the
  refresh cookie is only sent to `/api/v1/auth`. So a signed-in page first shows
  "Checking your session…", and a visitor without a session is sent to
  `/login?next=<page>`. The guard only decides what is SHOWN; every API call is still
  checked by the backend.
- **One place navigates after a login**: `RedirectIfSignedIn` in the `(auth)` layout
  moves a signed-in visitor on, so the forms only start the session. Logout doesn't
  navigate either: the guard sees the session end.
- **`?next=` is checked against open redirects** (`src/lib/safe-redirect.js`): only a
  path on this site is followed; `//host`, `/\host`, full URLs and the login pages
  themselves fall back to `/`.
- **Registering logs in automatically** (a second request, with the same details).
  Register and login have separate rate-limit counters. If only the login fails, the
  form says the account exists and asks to log in.
- **Instant-navigation validation is limited to pages that ask for it**
  (`experimental.instantInsights.validationLevel: "manual-warning"`). Next.js checks
  in development that every page prerenders into a static shell; signed-in pages never
  can, because the server only ever renders the guard. Found during the live check,
  where it showed as a development error on every signed-in page.
- Checked live, in a real browser with the API and worker: register → logged in;
  reload → session restored; `/login` while signed in → home; logout → `/login`;
  wrong password → the generic message; nothing in `localStorage`, `sessionStorage`
  or readable cookies; the user deleted afterwards with `DELETE /me`; no email, name,
  password or token in the API's or worker's logs.

### Review of PR 31 (2026-11-03)
- **One submit at a time.** A double click or a double Enter submitted the login and
  register forms twice, because React state only changes on the next render. A
  `useRef` flag, set synchronously, now blocks the second submit; a second
  `POST /auth/register` used to answer 409 for the account the first had just made.
- **`?next=` keeps the query string.** The guard reads the full address
  (`window.location.pathname + search`) inside its effect, so filters such as
  `?month=2026-10` survive a login. `useSearchParams()` would have needed a
  `<Suspense>` boundary around every signed-in page.
- **An explicit logout goes to plain `/login`.** The auth context records
  `loggedOut` when the session ends through `logout()`, so the next person at a
  shared computer finds nothing of the previous user's page in the URL. An expired
  session still gets `?next=`.

## Phase 3 Step 5: accounts (2026-11-03)
- **Only the last 4 digits leave the browser.** The add form checks for exactly 4
  digits in code before sending, not only with the input's `pattern` (which a script
  can bypass), so a pasted full account number is refused on the page. No `maxLength`
  either: it would silently cut a pasted 12-digit number down to its FIRST 4 digits.
- **Deleting is confirmed inline**, in the account's row: the warning that its
  statements and transactions go too, "Delete account" and "Cancel", with focus on
  "Cancel" so a stray Enter deletes nothing. A native `<dialog>` was the first plan,
  but jsdom (the test browser) has no `showModal()`, so it couldn't be tested as it
  really runs.
- **The bank and the last 4 digits can't be edited** (the backend's rule: they say
  which real account this is); the edit form says to delete and re-add instead.
- **After every change the data is fetched again** (TanStack Query invalidation)
  rather than patched locally. Deleting an account invalidates ALL cached data, since
  its uploads and transactions are gone too.
- **One shared `Button`** (`type="button"` by default, so a button inside a form
  never submits it by accident).
- Checked live: first-run card → a 12-digit number refused with no request sent → the
  account added → welcome; rename and retype (PATCH 200); delete with the warning and
  focus on Cancel; the test user deleted afterwards; nothing of it in the logs.

### Review of PR 32 (2026-11-03)
- **A 404 on edit or delete refetches the list.** The account was already gone
  (deleted in another tab); the dead row used to stay, and every retry said "Account
  not found.". Done in the hooks (`src/lib/accounts.js`), so both actions get it.
- **Each row's buttons are named after the account** ("Edit Salary account",
  "Delete Salary account"). With several accounts a screen reader used to list
  identical "Edit, Delete" buttons. The names start with the visible word, so voice
  control ("click Delete") still finds them.
- **The delete confirmation is a `role="alertdialog"` described by its warning.**
  Focus moves to "Cancel"; as a plain group a screen reader announced only "Cancel,
  button", never what gets deleted.

## Phase 3 Step 6: uploads (2026-11-03)
Decided with Dhruv when the step started:
- **The browser refuses a file over 10 MB before sending it**, from a constant
  (`MAX_UPLOAD_MB` in `src/lib/uploads.js`) that mirrors the backend's default
  `MAX_UPLOAD_MB`. Rejected: a `NEXT_PUBLIC_` variable (one more value to keep in step
  by hand per deployment) and a limits endpoint (backend work for one number). The
  check matters because the Next.js proxy answers a bare 500 above 12 MB (see the
  review of PR 30), so the backend's readable 413 never arrives for a big file. The
  backend still enforces its own limit; the two numbers must be changed together.
- **Polling follows the API's `Retry-After`.** Each `pending`/`processing` upload has
  its own TanStack Query poll of `GET /uploads/{id}` whose `refetchInterval` is the
  header's value; a final answer has no header, so polling stops by itself. The API
  client gained `withRetryAfter: true` to return that header of a successful answer.
  Every poll answer is written into the cached LIST, so the uploads page and the home
  page update from one source; a final status refetches every other query (new
  transactions change other pages). Rejected: refetching the whole list every 2 s
  (counts against the general 100/min limit and ignores what the API asks for).
- **Uploads get their own page, `/uploads`**, and the home page's first run goes
  account → first statement → the latest upload's result.
- **The password field appears only once a PDF is chosen**, and is optional. A locked
  PDF sent without it is stored as `needs_password` (409 `PASSWORD_REQUIRED`), and its
  row in the list shows a password prompt; the same prompt serves a locked upload
  found after a reload. A wrong password at upload time stores nothing (400), so the
  form keeps the file and asks only for the password again. Every password field is
  emptied after each attempt, right or wrong.

Also in this step:
- **`.gitignore` exception `!frontend/src/**/uploads/`.** The data rule `uploads/`
  matches a folder of that name anywhere, so the new page and component folders were
  silently left out of `git status`. The exception re-includes only source folders
  under `frontend/src/`; `uploads/` at the root or in `backend/` stays ignored
  (checked with `git check-ignore`).
- **A 429's wait is said in minutes above one minute** (`form-errors.js`): the upload
  limit is per hour, and "3412 seconds" helped nobody.
- **Statement dates are formatted as UTC dates** (`src/lib/dates.js`): `new
  Date("2026-08-01")` is midnight UTC, which a browser west of UTC shows as 31 July.
  Timestamps are shown in Asia/Kolkata.
- **A notice above the form says only what is still true.** "…is being imported" and
  "…is password-protected" are tied to their upload in the cached list and disappear
  once it is imported, unlocked or deleted. Found in the live check: the password
  notice stayed on the page after its upload had been unlocked and deleted.
- Checked live with synthetic files (the fixtures' ICICI `.xls` and a locked PDF): an
  11 MB file refused with no request sent; first run account → first statement →
  imported counts on the home page, polled at the API's 2 s; the locked PDF → 409 →
  prompt → wrong password (field emptied) → right one → imported as 5 rows already
  there; deleting the earlier `.xls` refused with the "later statement" 409, then
  allowed after the PDF was deleted. Test user deleted with `DELETE /me`; no
  password, narration or amount in the API or worker logs.

### Review of PR 33 (2026-11-03)
Top 5 of 10 findings fixed:
- **Polling moved from each row to one watcher** (`UploadsWatcher` in the signed-in
  layout, `useUnfinishedUploadsPolling`). It reads the uploads list from the cache only
  and polls every unfinished upload in it, whichever page is open. Each row used to
  poll itself, so an upload not on screen (the home page shows only the latest; or the
  user left /uploads) was never polled, and its finish refreshed nothing. Checked live:
  an upload left on /uploads while "Waiting to import" was polled from the Accounts
  page and finished there.
- **The polls share the API's pace** (`pollDelayMs`). With n uploads waiting, each
  waits n × `Retry-After`, so the total stays at one request per `Retry-After` and
  can't reach the 120/minute poll limit (6 uploads at 2 s each were 180/minute). After a
  429 the 429's own `Retry-After` is used, not the last success's. Never 0, which
  TanStack Query reads as "no interval".
- **The upload form uses an account that is still in the list.** The picked id falls
  back to the first account when the picked one disappears (deleted in another tab), so
  the request goes to the account the drop-down shows instead of a deleted id (404).
- **Statement password fields use `autocomplete="one-time-code"`.** Browsers ignore
  `"off"` on password fields, so a saved Kharcha login could be filled in (and sent as
  the PDF's password) or the statement password offered for saving. Rejected:
  `"new-password"`, which makes Chrome suggest a generated password and offer to save
  the typed one as this site's new password. Browser behaviour: the tests only check
  the attribute.
- **The home page's first step needs only the accounts.** A failing GET /uploads used
  to replace the "Add your first account" step with an error.

Left as they are: the delete refetching its own poll (one extra 404 per delete), every
finished poll refetching all other queries, the duplicated delete confirmation
(accounts and uploads), the two `plural` helpers, and test helpers copied between test
files.

## Phase 3 Step 7: CSV column mapping (2026-11-03)
Decided with Dhruv when the step started:
- **Every CSV gets the mapping step**, as soon as one is chosen. No bank parser reads
  CSVs today, so a CSV without a mapping always failed in the worker; if a bank
  parser ever recognises one, the backend ignores the mapping (ADR 013 order).
  Rejected: mapping only after a failed upload (a failed upload, and one of the
  10/hour, every time); only for "Another bank" accounts (HDFC and SBI CSVs would fail).
- **Guessed from the column names, nothing stored.** Each choice is pre-filled from
  the file's labels (Txn Date, Narration/Particulars/Remarks, Withdrawal/Debit,
  Deposit/Credit, Amount, Balance) for the user to confirm. Rejected: remembering the
  mapping per account (localStorage is per browser; the backend option is a migration
  for a convenience).
- **Our own small reader, no new dependency** (`src/lib/csv-preview.js`). It reads
  only the first 64 KB and mirrors `app/parsers/readers.py` and `generic_csv.py`: UTF-16
  by byte-order mark, else UTF-8 (BOM dropped, streaming so a cut inside a character
  isn't "invalid"), else Windows-1252; NUL means unreadable; delimiter among `, ; tab |`
  by the most consistent cell count; RFC 4180 quoting; the header within the first 50
  rows. The header is the row with the most label-like cells (letters, no digits); the
  user can pick another row. Rejected: papaparse (a dependency whose delimiter and
  encoding choices could differ from the backend's, offering names it never finds).
- **Date formats: a preset list with a preview.** Eleven `strptime` patterns, each
  checked against the backend's own validator. `parseDateWith` reimplements Python's
  rules for the directives used (one- or two-digit `%d`/`%m`, case-insensitive month
  names, whole-cell match, impossible dates refused, the 69/68 two-digit-year pivot);
  every test case was checked against real `datetime.strptime`. A format is
  pre-selected only when exactly ONE reads all the first dates: "01/08/2026" fits both
  day-first and month-first, and the user must choose (the backend's no-guessing rule).
  The preview shows the dates as read ("1 Aug 2026" vs "8 Jan 2026").

Also:
- Mapping mistakes the backend would refuse (a missing column, a column used twice,
  debit without credit) and a format that doesn't read the file's dates are caught
  before sending, under their field. A 422 naming `column_mapping.<field>` lands under
  the same field.
- Amounts in the preview are the file's own text, never converted (hard constraint 4).
- Checked live with a synthetic CSV (title rows above the table, a quoted narration
  containing a comma, CRLF line endings): header found on row 4, every column and the
  day-first format guessed, imported by the real worker as 3 transactions with the
  balances reconciling. Test user deleted; nothing of the file in the API or worker logs.

### Review of PR 34 (2026-11-03)
Top 5 of 10 findings fixed:
- **One delimiter rule on both sides (backend change).** Python's `csv.Sniffer` gives
  up when any line lacks the delimiter (a bank's title line is enough) and the backend
  then fell back to a comma, so a semicolon, tab or pipe CSV with a title line was read
  as one cell per line: the browser showed a correct mapping and the import failed.
  `app/parsers/readers.py` now uses `_sniff_delimiter`, the browser's rule (most
  consistent cell count above 1 over the first 60 rows, ties to `, ; tab |` order,
  comma when nothing splits). `_SNIFF_ROWS` / `SNIFF_ROWS` must stay equal. Shared test
  files in both suites; checked live with the worker rebuilt from the branch.
- **The browser decodes the whole file, exactly as the backend.** UTF-8 vs
  Windows-1252 is decided on every byte, not the first 64 KB; broken UTF-16 is refused;
  the five bytes Python's cp1252 refuses (0x81 0x8D 0x8F 0x90 0x9D) are refused too
  (the browser's windows-1252 would have accepted them).
- **Date formats are judged on every row read** (up to 200 data rows), not the first 5.
- **Dates with a time can be mapped.** `parseDateWith` gained `%H %I %M %S %p` (Python's
  regexes printed from `_strptime.TimeRE`; second 60/61 refused as datetime does).
  When the dates carry a time, the list offers each date format joined with the time
  layouts that fit. Also added: `%b %d, %Y` ("Aug 01, 2026"). All 60 formats pass the
  backend's validator; every new case checked against `datetime.strptime`.
- **Pydantic's "Value error, " prefix is dropped** from field messages, in
  `formErrorsFrom` (every form).

Left as they are: tests for the stale-read guard and the header-row re-guess, the
duplicated initial-mapping expression and "first column with this name" rule, nested
422 fields handled in the upload form, and the misleading `isCsv` comment.

Found during the live check, then fixed on the same branch (decided with Dhruv): a
decimal-comma amount ("70,00") was read as digit grouping, so ₹70.00 became ₹7,000.00,
and a balance column scaled the same way hid it from reconciliation. Semicolon CSVs
(where decimal commas are usual) can now be imported, which made this reachable.
`parse_amount_to_paise` now accepts commas only as a real Indian (1,25,000) or
international (125,000) grouping and refuses anything else, so such a row fails with
its row number. Both real statements in `samples/` still parse in full.

## Phase 3 Step 8: transactions (2026-11-03)
Decided with Dhruv when the step started:
- **`uncategorized=true` on `GET /transactions` (backend addition).** Lists rows with no
  category (`category_id IS NULL`); with `category_id` too it is a 422 ("not both").
  This closes the Phase 1 open decision. Rejected: a magic `category_id=none` (the
  field stops being a plain UUID), and no such filter (the view manual categorization
  needs most).
- **"Apply to similar?" is asked after choosing a category,** inline in the row: "File
  every transaction from <UPI id or merchant> as <category>?" with "Yes, all similar",
  "Just this one" and "Cancel"; ONE PATCH carries the answer. Rows with neither a UPI id
  nor a merchant key aren't asked (the API would answer 400 NOTHING_TO_MATCH).
  Rejected: a checkbox by each drop-down (easy to miss or leave ticked).
- **No date range by default:** everything, newest first, 50 at a time with "Load
  more" (TanStack Query's useInfiniteQuery over the API's cursor). "This month" and
  "Last month" fill in the calendar month in India's time.
- **The "Guess" badge** marks `heuristic` rows with confidence ≤ 0.80, as planned;
  self transfers get their own badge.

Also:
- **Filters live in the URL** (`?category=…&q=…`), read with useSearchParams and
  written with router.push: bookmarkable, reload-proof, and the Back button returns to
  the previous filters. The form edits a draft and is re-created (`key` = the query)
  when the URL changes by itself. Invalid values in a hand-edited link are dropped
  rather than sent. The page wraps the view in Suspense, which `next build` requires
  around useSearchParams.
- **"Not categorized yet", not "Uncategorized",** for category_id null (badge,
  drop-down, filter). The seed has a real system category named "Uncategorized" (the
  user's "nothing fits"), so the null case uses the insights API's name for it. Found in
  the live check, where both appeared in one drop-down.
- After a PATCH the returned row replaces the cached one at once, then every list is
  refetched (the row may leave the filter); with apply_to_similar once more after 2 s,
  when the background job has usually re-filed the others.
- One markup for both layouts: a card per row on a phone, a CSS grid row lined up with
  hidden-on-phone column headings from `md`.
- Checked live with a synthetic import: the Uncategorized filter (`uncategorized=true`),
  "Yes, all similar" re-filing both payments to one merchant with one PATCH, Back
  restoring the previous filters and form, a note, and the phone layout without
  sideways scrolling. Test user deleted; nothing of it in the API or worker logs.

### Review of PR 37 (2026-11-04)
Top 5 of 10 findings fixed:
- **The API says what "similar" means (backend addition).** Every transaction now has
  `similar_key`: what `apply_to_similar` would match, a person's UPI id or a merchant
  key, null when nothing could (then the API answers 400). It is computed by
  `rules_service.similar_keys` from `_rule_match`, the code `learn_rule` itself uses,
  with the merchant dictionary read once per page. The browser used to guess, always
  preferring the UPI id, while the rule uses it only for a person: a Swiggy payment
  asked "from swiggy@…" and then matched the merchant SWIGGY. Integration tests assert
  the invariant: for each kind of row, the key shown equals the learned rule's pattern.
- **A refused filter says why.** A 422's top-level message is always "The request
  contains invalid data."; `filtersErrorMessage` shows the reasons from `details`
  instead, labelled with the filter for `query.<name>` ("Smallest amount: …"). The old
  test faked a 422 the API never sends; the new ones use shapes captured from it.
- **Rows wait for the categories**, so a row's drop-down can't show "Not categorized
  yet" for a category it can't find yet.
- **What a save did is said above the list,** in one status line, not inside the row:
  in a filtered list the saved row leaves at once and took its message with it.
- **After "apply to similar" the lists are refetched until the job's work shows**
  (after 1, 2, 4 and 8 s at most; `refreshUntilSettled`), judged by `similar_key`: no
  cached row with that key and another category, except rows filed by hand. It used to
  be one refetch after a guessed 2 s.

Left as they are: the quick month buttons skipping the amount fields' validation, the
duplicated account label and badge styling, the per-row category grouping, and a test
for the refetch loop inside the hook (its pieces are unit-tested).

## Phase 3 Step 9: dashboard (2026-11-04)
The home page becomes the month at a glance once there is an upload: a month picker,
money in / money out / left over against last month (`GET /insights/summary`), "N
transactions need your attention", and where the money went as a donut plus a list
(`GET /insights/categories`). Decided with Dhruv when the step started:

- **"Needs your attention" counts every uncategorized transaction, in and out (backend
  addition).** The summary's `PeriodTotals` gains `uncategorized_count`, one more
  `COUNT(*) FILTER (WHERE category_id IS NULL)` in the same query, so no extra round
  trip. The donut's "Not categorized yet" bucket was rejected as the source: it counts
  debits only, so an unfiled salary or refund would never ask for attention. Self
  transfers are excluded like every other total, which removes nothing in practice: a
  row without a category never has the flag (both places that clear a category clear
  it), so the count equals what the linked `/transactions?category=uncategorized` lists.
- **The dashboard opens on the newest transaction's month** (one
  `GET /transactions?limit=1`), not the current month: statements usually cover a month
  that has ended, and on the 4th the current month is nearly empty. With no
  transactions it opens on the current month (India's time). A `?month=` in the URL wins,
  unless it is malformed or after the current month; the picker can't go past it either.
- **"Guess" rows don't count as needing attention**: they have a category, and the
  transactions page already marks them.
- **The month is in the URL** (`/?month=2026-10`, `router.push`), for the same reasons as
  the transaction filters: reload, bookmark, Back.
- **The list is the content, the chart a picture of it.** Each category is a row (colour
  dot, name, count, the API's `amount_display` and `share_pct`) linking to that month's
  debits in that category; the chart is `aria-hidden` with Recharts'
  `accessibilityLayer={false}`, so keyboard and screen-reader users meet the data once.
  Fixed 200 × 200 px, not `ResponsiveContainer`, which measures a 0 × 0 parent before
  layout and in jsdom. No animation. "Not categorized yet" is a plain grey,
  unlike the seeded "Uncategorized" category's bluish #9CA3AF (#8C8C8C since the review
  below).
- **No money arithmetic in the browser** (hard constraint 4): every amount is a
  `*_display` from the API, every percentage the API's own, only formatted (one decimal
  at most). `net_paise` is compared with zero only, to choose "Left over" or
  "Overspent". A null `*_change_pct` says "Nothing came in during September" instead of
  a number (ADR 010).
- **The latest-upload card stays above the dashboard only while it needs something**
  (importing, password, check, failed), or when the user watched it import on this page,
  so the first run still shows "31 new transactions added" next to the dashboard. That
  is remembered with React's "adjust state while rendering" pattern rather than an
  effect, which would let the card vanish for a frame.
- **New dependencies:** `recharts` 3.10.1 (released 2026-07-25; React 19 among its
  peers) and its peer `react-is` 19.3.1, pinned to match React. `npm audit --omit=dev`
  reports nothing.
- Checked live with a synthetic two-month CSV: every total and percentage matched the
  figures worked out by hand, the attention link listed exactly the 2 rows it counted,
  filing one of them brought the count to 1 on return, September's comparison with an
  empty August read "Nothing came in during August", November showed the empty state
  with "›" disabled, and the phone layout had no sideways scrolling. Test user deleted;
  the API and worker logs held only ids, counts and event names.

### Review of PR 38 (2026-11-07)
Seven of eight findings fixed:
- **No "upload that month's statement" while the first statement imports.** With no
  transactions yet the dashboard opened on the current month and asked for an upload
  beneath the "Importing" card. It now says the months appear once a statement is
  imported.
- **A month with no money in or out isn't called empty**: it may hold only self
  transfers (left out of every total). It says what is counted and asks "Missing this
  month's statement? Upload it".
- **The first month the API accepts is respected.** The summary takes years from 1900
  and reports the month before, so `?month=` before 1900-02 now falls back like any
  other bad value, and "‹" is disabled there.
- **"Not categorized yet" is #8C8C8C**, 3.4:1 on white and 5.9:1 on the dark background;
  the earlier #D4D4D4 was 1.5:1 on white in light mode.
- **One `plural` helper** (`src/lib/text.js`) replaces the copies in form-errors.js and
  upload-item.jsx and the dashboard's own `transactionCount`.
- **One fake insights API for the tests** (`fakeInsightsHandler`), and the fake
  summary's `to` is the month's real last day.

Left as it is: the breakdown is requested even for a month that turns out empty.
Waiting for the summary first would make every non-empty month load in two steps.

## Phase 3 Step 10: settings (2026-11-07)
A Settings page (`/settings`, linked in the header): the profile from `GET /me`, and
deleting the account through `DELETE /me` (ADR 021). No backend change. Decided with
Dhruv when the step started:

- **Typed confirmation: the word `DELETE`**, exactly, plus the current password. The
  delete button stays disabled until both are filled in. Rejected: typing the email
  (slower, and a browser may autofill it). Only the password is sent; the typed word
  stays in the browser.
- **"Your account has been deleted" is a flag in the session context**
  (`accountDeleted`, beside `loggedOut`), not `/login?deleted=1`: a reload or a bookmark
  of the login page must not announce a deletion again. The next login clears it.
- **No AI or weekly-email switches yet.** `PATCH /me/settings` and the plain-language
  explanation of the AI step (CLAUDE.md §7.4) are Phase 4; a switch that can't be
  changed would only confuse.
- `useAuth().deleteAccount(password)` sits beside `login` and `logout`, so every way a
  session starts or ends is in src/lib/auth.jsx. It ends the session only after the
  204; a wrong password (400 `INCORRECT_PASSWORD`, deliberately not 401) or a 429 throws
  first, the session goes on, the password field shows the error and is cleared.
- The guard then sends the browser to plain `/login`, as after a logout.

**Found in the live check: the login form kept the previous user's password.**
`cacheComponents` (Step 3) makes Next.js keep a page that was left mounted but hidden,
with its React state, so Back can show it at once. The login form left behind after a
successful login still held the email and password, and "Logging in…". After a logout
or an account deletion, `/login` showed them again. This was there since Step 4, and
the deletion flow made it visible. Fixed at the source: the `(auth)` layout wraps its
page in `SignedOutOnly`, which unmounts the form as soon as a session starts (it shows
"Signing you in…" while `RedirectIfSignedIn` navigates), so a session that ends always
comes back to an empty form. Rejected: clearing the fields inside each form, which
every future form with a secret would have to remember. The signed-in pages need
nothing: `RequireAuth` already unmounts them when the session ends.

Checked live with throwaway users:
- A wrong password kept the session and cleared the field.
- The real password deleted the user's account and refresh-token rows (0 left), with
  one `user_deleted` audit row each.
- `/login` showed the notice above an empty form, and not again after a reload.
- The old refresh cookie got 401.
- Logging out also left an empty form.
- The Settings page and the five-link header fit a phone width.
- The API logs held no email or password.
