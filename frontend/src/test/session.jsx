// Test helpers: render a component inside the app's providers, with a fake
// API instead of the network. Not a test file itself (no ".test" in the name).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { vi } from "vitest";

import { createApiClient } from "@/lib/api";
import { AuthProvider } from "@/lib/auth";

export function json(status, body, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

export function apiError(status, code, message, extra = {}) {
  const { details = null, headers = {} } = extra;
  return json(status, { error: { code, message, details } }, headers);
}

/**
 * Render `ui` with a QueryClient and an AuthProvider whose API client calls
 * `routes[path](options)` instead of the network, e.g.
 * `{ "/auth/login": () => json(200, {...}) }`. Paths without a handler
 * answer 404.
 *
 * `signedIn`: whether the session restore on page load (POST /auth/refresh)
 * succeeds, unless `routes` defines /auth/refresh itself.
 *
 * `handler(path, options)`: asked first, for fakes that need the method or a
 * path with an id in it (e.g. PATCH /accounts/<id>); return undefined to
 * fall through to `routes`.
 */
export function renderWithSession(ui, { signedIn = false, routes = {}, handler } = {}) {
  const fetchImpl = vi.fn(async (url, options = {}) => {
    const path = url.replace("/api/v1", "");
    const handled = handler?.(path, options);
    if (handled !== undefined) {
      return handled;
    }
    if (routes[path]) {
      return routes[path](options);
    }
    if (path === "/auth/refresh") {
      return signedIn
        ? json(200, { access_token: "token-1", token_type: "bearer", expires_in: 900 })
        : apiError(401, "UNAUTHORIZED", "Invalid or expired refresh token.");
    }
    return apiError(404, "NOT_FOUND", "Not found.");
  });
  const client = createApiClient({ fetchImpl, locks: null });
  // retry: false, so a failing query fails at once instead of after retries.
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  const result = render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider client={client}>{ui}</AuthProvider>
    </QueryClientProvider>,
  );
  return { ...result, client, fetchImpl, queryClient };
}

/** The JSON body a fake fetch call was sent with. */
export function sentBody(fetchImpl, path) {
  const call = fetchImpl.mock.calls.find(([url]) => url === `/api/v1${path}`);
  return call ? JSON.parse(call[1].body) : undefined;
}
