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

async function toApiError(response) {
  let body = null;
  try {
    body = await response.json();
  } catch {
    // Not JSON (e.g. a proxy error page): fall back to a generic message.
  }
  const info = body?.error ?? {};
  const retryAfter = Number.parseInt(response.headers.get("Retry-After") ?? "", 10);
  return new ApiError({
    status: response.status,
    code: info.code ?? "HTTP_ERROR",
    message: info.message ?? `The request failed (HTTP ${response.status}).`,
    details: info.details ?? null,
    retryAfterSeconds: Number.isNaN(retryAfter) ? null : retryAfter,
  });
}

/**
 * Build an API client. The app uses the one `api` below; tests build their
 * own with a fake `fetchImpl`, so no real network is involved.
 */
export function createApiClient({ fetchImpl = (...args) => fetch(...args) } = {}) {
  let accessToken = null;
  // The refresh in progress, if any (a Promise), shared by every caller.
  let refreshInFlight = null;
  // Functions to call whenever the token changes (the auth context's
  // re-render hook, see src/lib/auth.jsx).
  const listeners = new Set();

  function setAccessToken(token) {
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
   */
  function refreshSession() {
    if (refreshInFlight === null) {
      refreshInFlight = sendRefresh().finally(() => {
        refreshInFlight = null;
      });
    }
    return refreshInFlight;
  }

  async function sendRefresh() {
    // No Authorization header: the refresh cookie is the credential. The
    // browser sends it by itself (same origin, path /api/v1/auth).
    const response = await fetchImpl(`${API_BASE}${REFRESH_PATH}`, {
      method: "POST",
      credentials: "same-origin",
    });
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
   */
  async function request(path, { method = "GET", body, headers = {}, signal } = {}) {
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
    if (response.status === 204) {
      return null;
    }
    return response.json();
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
