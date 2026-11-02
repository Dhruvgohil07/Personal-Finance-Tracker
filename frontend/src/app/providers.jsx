"use client";

// Everything the pages need from the outside, wrapped around the whole app
// in layout.jsx. "use client": providers hold state and run effects, which
// only works in the browser; layout.jsx itself stays a server component.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError } from "@/lib/api";
import { AuthProvider } from "@/lib/auth";

// TanStack Query fetches, caches and re-fetches API data for the pages.
// A failed query is retried once, but never for a 4xx: "not found" or
// "invalid input" will not change by asking again.
function shouldRetry(failureCount, error) {
  if (error instanceof ApiError && error.status < 500) {
    return false;
  }
  return failureCount < 1;
}

export function Providers({ children }) {
  // One QueryClient per browser tab, created on the first render only:
  // useState's initializer function runs once, so a re-render doesn't
  // throw the cache away.
  const [queryClient] = useState(
    () => new QueryClient({ defaultOptions: { queries: { retry: shouldRetry } } }),
  );

  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>{children}</AuthProvider>
    </QueryClientProvider>
  );
}
