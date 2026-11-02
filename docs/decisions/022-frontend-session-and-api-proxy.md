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
