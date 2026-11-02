"use client";

// The logged-in user's own profile (GET /me), loaded once and cached by
// TanStack Query under the key ["me"]: every component that calls useMe()
// shares the same request and the same answer.

import { useQuery } from "@tanstack/react-query";

import { useAuth } from "@/lib/auth";

export const ME_QUERY_KEY = ["me"];

/** `{ data, isPending, error }` for GET /me; only asks while signed in. */
export function useMe() {
  const { status, client } = useAuth();
  return useQuery({
    queryKey: ME_QUERY_KEY,
    // `signal` lets TanStack Query cancel the request if nobody needs the
    // answer any more (e.g. the page was left).
    queryFn: ({ signal }) => client.request("/me", { signal }),
    // `enabled: false` means "don't fetch": without a session it would 401.
    enabled: status === "authenticated",
  });
}
