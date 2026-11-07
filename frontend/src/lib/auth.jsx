"use client";

// The session, as React sees it (ADR 022).
//
// React "context" passes a value down to every component inside a provider,
// without handing it through each component in between as a prop. Pages call
// `useAuth()` to read the session and to log in or out.
//
// The access token itself lives in the API client (src/lib/api.js), in
// memory. This file only mirrors it into React, so components re-render when
// it changes.

import { useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import { api as defaultClient } from "@/lib/api";

const AuthContext = createContext(null);

/**
 * Provides the session to everything inside it.
 *
 * `client` is the API client; tests pass their own with a fake fetch.
 */
export function AuthProvider({ children, client = defaultClient }) {
  const queryClient = useQueryClient();

  // useSyncExternalStore: the React hook for reading a value kept OUTSIDE
  // React (here, the token in the API client). React calls `subscribe` to
  // be told about changes and `getAccessToken` to read the current value.
  // The third function is the value while rendering on the server, where
  // there is never a token.
  const accessToken = useSyncExternalStore(client.subscribe, client.getAccessToken, () => null);

  // True until the first silent refresh has answered.
  const [restoring, setRestoring] = useState(true);

  // An "effect" runs after the component is shown, here once per page load:
  // the token is only in memory, so a reload starts without one, and the
  // httpOnly refresh cookie (which the browser keeps) gets a new one.
  useEffect(() => {
    let active = true;
    client
      .refreshSession()
      // A network error or a 500 while restoring: treat as logged out
      // rather than leaving the app stuck on "loading".
      .catch(() => false)
      .finally(() => {
        if (active) {
          setRestoring(false);
        }
      });
    // The cleanup function: runs if the component goes away before the
    // refresh answers, so we don't update a component that no longer exists.
    return () => {
      active = false;
    };
  }, [client]);

  // When a session ends (logout, or a refresh the API refused), forget every
  // cached API answer, so the next person at this browser never sees the
  // previous user's data. useRef keeps a value between renders without
  // causing one.
  const previousToken = useRef(accessToken);
  useEffect(() => {
    if (previousToken.current !== null && accessToken === null) {
      queryClient.clear();
    }
    previousToken.current = accessToken;
  }, [accessToken, queryClient]);

  // useCallback keeps the same function between renders, so components
  // that receive it don't re-render for nothing.
  // True when the session ended because the user clicked "Log out", rather
  // than because it expired. The guard then sends them to plain /login,
  // without ?next=<the page they were on>: on a shared computer, the next
  // person shouldn't find the previous user's page in the URL (review of
  // PR 31). Cleared by the next login.
  const [loggedOut, setLoggedOut] = useState(false);
  // True when the session ended because the account was deleted
  // (deleteAccount below), so the login page can say so. Kept here, not in
  // the URL (decided with Dhruv): a reload or a bookmark of /login must not
  // announce a deletion again. Cleared by the next login.
  const [accountDeleted, setAccountDeleted] = useState(false);

  const login = useCallback(
    async (email, password) => {
      const body = await client.request("/auth/login", {
        method: "POST",
        body: { email, password },
      });
      setLoggedOut(false);
      setAccountDeleted(false);
      client.setAccessToken(body.access_token);
    },
    [client],
  );

  const logout = useCallback(async () => {
    try {
      // Revokes the refresh token on the server and clears the cookie.
      await client.request("/auth/logout", { method: "POST" });
    } finally {
      // Even if that call fails, this browser forgets the session. Both
      // updates happen in the same tick, so React renders them together:
      // the guard never sees "anonymous" without "loggedOut".
      setLoggedOut(true);
      client.setAccessToken(null);
    }
  }, [client]);

  // DELETE /me (ADR 021): the password is asked for again, and the API
  // deletes the user and everything that belongs to them, then clears the
  // refresh cookie. Only on that success does this browser end the session,
  // the same way logout() does, plus the flag for the login page. A wrong
  // password (400) or a rate limit (429) throws before any of it: the
  // session goes on, and the form shows the error.
  const deleteAccount = useCallback(
    async (password) => {
      await client.request("/me", { method: "DELETE", body: { password } });
      setLoggedOut(true);
      setAccountDeleted(true);
      client.setAccessToken(null);
    },
    [client],
  );

  let status = "anonymous";
  if (accessToken !== null) {
    status = "authenticated";
  } else if (restoring) {
    status = "loading";
  }

  // useMemo: a new object only when something in it changed, so components
  // reading the context don't re-render on every render of this one.
  // `client` is passed on so hooks such as useMe() call the API through the
  // same client (and its token) as the session itself.
  const value = useMemo(
    () => ({ status, loggedOut, accountDeleted, login, logout, deleteAccount, client }),
    [status, loggedOut, accountDeleted, login, logout, deleteAccount, client],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/**
 * The session: `{ status, loggedOut, accountDeleted, login, logout,
 * deleteAccount, client }`.
 * status is "loading" | "authenticated" | "anonymous"; loggedOut is true
 * when the session ended through logout() or deleteAccount() (not by
 * expiring), and accountDeleted only after deleteAccount().
 */
export function useAuth() {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error("useAuth() must be used inside <AuthProvider>");
  }
  return value;
}
