// The one place the frontend talks to the API (ADR 022).
//
// What it does for every call:
// - sends it to /api/v1/... on this same origin (next.config.mjs forwards it);
// - adds `Authorization: Bearer <access token>`;
// - turns the API's error shape {"error": {code, message, details}} into an
//   ApiError the pages can show;
// - on a 401 (the 15-minute access token has expired), gets a new access
//   token from POST /auth/refresh ONCE and repeats the call once.
//
// Where the access token lives: in a variable inside the client, in memory
// only, never in localStorage, sessionStorage or a readable cookie (Phase 3
// plan, hard constraint 1). A script injected into the page (XSS) can't
// read a token that was never stored. After a page reload it is gone, and
// the session is restored with one silent refresh: the refresh token is an
// httpOnly cookie the browser sends by itself.

export const API_BASE = "/api/v1";
const REFRESH_PATH = "/auth/refresh";

/** An error answer from the API, with the fields of its error shape. */
export class ApiError extends Error {
  constructor({ status, code, message, details = null, retryAfterSeconds = null }) {
    super(message);
    this.name = "ApiError";
    this.status = status; // the HTTP status, e.g. 404
    this.code = code; // stable, machine-readable, e.g. "NOT_FOUND"
    this.details = details; // e.g. which fields failed validation
    // From the Retry-After header of a 429: how long to wait, in seconds.
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

// The `Retry-After` header in seconds, or null when there is none. The API
// sends it on a 429 (how long to wait) and while an upload is unfinished
// (when to poll again).
function retryAfterSecondsOf(response) {
  const seconds = Number.parseInt(response.headers.get("Retry-After") ?? "", 10);
  return Number.isNaN(seconds) ? null : seconds;
}

async function toApiError(response) {
  let body = null;
  try {
    body = await response.json();
  } catch {
    // Not JSON (e.g. a proxy error page): fall back to a generic message.
  }
  const info = body?.error ?? {};
  return new ApiError({
    status: response.status,
    code: info.code ?? "HTTP_ERROR",
    message: info.message ?? `The request failed (HTTP ${response.status}).`,
    details: info.details ?? null,
    retryAfterSeconds: retryAfterSecondsOf(response),
  });
}

// The name of the browser-wide lock that refreshes run under (see below).
export const REFRESH_LOCK_NAME = "kharcha-refresh";

// The browser's Web Locks API, shared by every tab of this site, or null
// where it doesn't exist (very old browsers, and the jsdom test browser).
function browserLocks() {
  return typeof navigator !== "undefined" ? (navigator.locks ?? null) : null;
}

/**
 * Build an API client. The app uses the one `api` below; tests build their
 * own with a fake `fetchImpl` (and `locks`), so no real network is involved.
 */
export function createApiClient({
  fetchImpl = (...args) => fetch(...args),
  locks = browserLocks(),
} = {}) {
  let accessToken = null;
  // The refresh in progress, if any (a Promise), shared by every caller.
  let refreshInFlight = null;
  // Counts every change of session (login, logout, refresh). A refresh
  // compares it before and after its round trip, to notice that the user
  // logged in or out while it was on its way (see sendRefresh).
  let sessionGeneration = 0;
  // Functions to call whenever the token changes (the auth context's
  // re-render hook, see src/lib/auth.jsx).
  const listeners = new Set();

  function setAccessToken(token) {
    sessionGeneration += 1;
    accessToken = token;
    for (const listener of listeners) {
      listener();
    }
  }

  /**
   * Get a new access token with the refresh cookie. Resolves to true if the
   * session goes on, false if it has ended (the API answered 401).
   *
   * **Only one refresh at a time.** Refresh tokens rotate: each use
   * replaces the cookie with a new one. Two refreshes sent with the same
   * cookie would look like a stolen token being replayed, and the backend
   * would end the whole session (ADR 005). So when three requests get a
   * 401 together, they all wait for the SAME refresh, and only one is sent.
   *
   * **Also across tabs.** The variable above is per tab, but the cookie is
   * shared by every tab. Two tabs refreshing at the same moment (a browser
   * restoring its tabs, two tabs reloaded together) would send the same
   * cookie twice, and the backend refuses the second, which used to log
   * that tab out (review of PR 30). So the refresh runs inside a Web Lock:
   * a browser-wide "only one at a time" that other tabs wait for. A tab
   * that waited then refreshes with the cookie the other tab just received,
   * which is an ordinary rotation, not a reuse.
   */
  function refreshSession() {
    if (refreshInFlight === null) {
      const started =
        locks === null ? sendRefresh() : locks.request(REFRESH_LOCK_NAME, () => sendRefresh());
      refreshInFlight = started.finally(() => {
        refreshInFlight = null;
      });
    }
    return refreshInFlight;
  }

  async function sendRefresh() {
    const generationAtStart = sessionGeneration;
    // No Authorization header: the refresh cookie is the credential. The
    // browser sends it by itself (same origin, path /api/v1/auth).
    const response = await fetchImpl(`${API_BASE}${REFRESH_PATH}`, {
      method: "POST",
      credentials: "same-origin",
    });
    if (sessionGeneration !== generationAtStart) {
      // The user logged out (or in) while this refresh was on its way. Its
      // answer belongs to the old session and must not undo that: a late
      // new token would put a logged-out user back "in" (review of PR 30).
      // Its cookie is dead anyway: logout revoked the whole token family.
      return accessToken !== null;
    }
    if (response.status === 401) {
      // No valid refresh cookie: logged out, expired, or revoked.
      setAccessToken(null);
      return false;
    }
    if (!response.ok) {
      // Anything else (a 500, a 429) says nothing about the session, so it
      // is NOT ended; the caller sees the error instead.
      throw await toApiError(response);
    }
    const body = await response.json();
    setAccessToken(body.access_token);
    return true;
  }

  /**
   * Call the API. `body` may be a plain object (sent as JSON) or FormData
   * (a file upload). Resolves to the parsed JSON, or null for a 204.
   *
   * `withRetryAfter: true` resolves to `{ data, retryAfterSeconds }`
   * instead, for the one caller that needs a header of a SUCCESSFUL answer:
   * upload polling, which waits as long as the API says (src/lib/uploads.js).
   */
  async function request(
    path,
    { method = "GET", body, headers = {}, signal, withRetryAfter = false } = {},
  ) {
    const isForm = typeof FormData !== "undefined" && body instanceof FormData;

    async function send() {
      const tokenSent = accessToken;
      const response = await fetchImpl(`${API_BASE}${path}`, {
        method,
        signal,
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          // FormData sets its own Content-Type (with the multipart boundary).
          ...(body !== undefined && !isForm ? { "Content-Type": "application/json" } : {}),
          ...(tokenSent ? { Authorization: `Bearer ${tokenSent}` } : {}),
          ...headers,
        },
        body: body === undefined || isForm ? body : JSON.stringify(body),
      });
      return { response, tokenSent };
    }

    let { response, tokenSent } = await send();

    // The auth routes answer 401 for wrong credentials, not for an expired
    // token, so they are never refreshed-and-retried.
    if (response.status === 401 && !path.startsWith("/auth/")) {
      // If another call already refreshed the token while this one was on
      // its way, just use the new token: refreshing again would rotate the
      // cookie for nothing.
      const alreadyRefreshed = accessToken !== null && accessToken !== tokenSent;
      if (alreadyRefreshed || (await refreshSession())) {
        ({ response } = await send());
      }
    }

    if (!response.ok) {
      throw await toApiError(response);
    }
    const data = response.status === 204 ? null : await response.json();
    return withRetryAfter ? { data, retryAfterSeconds: retryAfterSecondsOf(response) } : data;
  }

  return {
    request,
    refreshSession,
    getAccessToken: () => accessToken,
    setAccessToken,
    /** Call `listener` whenever the token changes; returns "unsubscribe". */
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

/** The client the app uses. */
export const api = createApiClient();
