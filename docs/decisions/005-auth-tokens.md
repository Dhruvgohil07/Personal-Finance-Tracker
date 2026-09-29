# ADR 005 — Auth tokens: short JWT + rotating opaque refresh token

- Status: accepted
- Date: 2026-09-29 (Phase 1, Step 2)

## Context
The API needs login sessions that (a) don't hit the database on every
request, (b) can be ended (logout, stolen token) and (c) survive an XSS bug
in the frontend as well as possible. SPEC §7.1 fixes the outline: a 15 min
access JWT in the response body, a refresh token in an httpOnly cookie,
rotation on every refresh and family-based reuse detection.

## Decision
1. **Access token**: HS256 JWT, 15 min, claims `sub` (user id), `type`,
   `iat`, `exp`. Sent as `Authorization: Bearer`. Verified without the DB;
   `get_current_user` then loads the user so a deleted user is rejected
   immediately.
2. **Refresh token**: 32 random bytes (`secrets.token_urlsafe`), **not** a
   JWT. The DB stores only `HMAC-SHA256(JWT_REFRESH_SECRET, token)`. Valid
   30 days.
3. **Cookie**: `refresh_token`, `HttpOnly`, `Secure`, `SameSite=Strict`,
   `Path=/api/v1/auth`, so it is sent only to the auth routes.
4. **Rotation**: every refresh revokes the presented token and issues a new
   one in the same `family_id`. The row is read with `SELECT … FOR UPDATE`
   so two concurrent refreshes can't both succeed.
5. **Reuse detection**: presenting an already-revoked token revokes every
   token of that family and returns 401. Other families (other devices) are
   untouched.
6. **Logout** revokes the session's family and deletes the cookie. It
   needs no access token and always returns 204.
7. **Login** answers "Invalid email or password." for both unknown email and
   wrong password, and verifies against a dummy hash when the email is
   unknown, so response time doesn't reveal registered emails.
8. **Register** returns 201 with the user (no tokens); the client then logs
   in. A duplicate email returns 409, detected by the DB unique constraint
   (race-free), not by a check-then-insert.

## Alternatives rejected
- *Refresh token as a JWT*: rotation and revocation need a DB row anyway,
  so the signature adds nothing but size and a second secret to reason about.
- *Plain SHA-256 of the refresh token*: works, but with HMAC a leaked DB
  alone can't be used to confirm guessed tokens.
- *Access token in a cookie too*: would need CSRF protection on every
  route; a Bearer header isn't sent automatically by the browser.
- *Refresh token in the JSON body / localStorage*: readable by any script
  on the page, so one XSS bug would leak long-lived sessions.
- *Log the user in on register*: saves one request, but keeps register
  simpler and gives one single place (login) where sessions start.
- *403 from `HTTPBearer` when the header is missing (FastAPI default)*:
  replaced by our 401 in the standard error shape.

## Consequences
- A stolen access token works until it expires (max 15 min); there is no
  access-token blocklist.
- Register's 409 reveals that an email is registered. Accepted: login
  hides it, and register will be rate-limited (Step 3).
- Refresh errors don't clear the cookie (the exception path discards the
  response headers); the client simply gets 401 and logs in again.
- Revoked/expired rows accumulate; a cleanup job can delete rows expired
  for more than 30 days (Phase 5).
- Browsers treat `http://localhost` / `127.0.0.1` as secure, so the `Secure`
  cookie works in local development and in `/docs`; tests use an
  `https://testserver` base URL.
