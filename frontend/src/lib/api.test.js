import { describe, expect, it, vi } from "vitest";

import { ApiError, createApiClient } from "@/lib/api";

// A stand-in for fetch: answers each request with `handler(url, options)`,
// and records every call so the tests can check what was sent.
function fakeFetch(handler) {
  const calls = [];
  const fetchImpl = vi.fn(async (url, options = {}) => {
    calls.push({ url, options });
    return handler(url, options, calls);
  });
  return { fetchImpl, calls };
}

function json(status, body, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

const unauthorized = () =>
  json(401, { error: { code: "UNAUTHORIZED", message: "Invalid or expired token." } });

const bearer = (call) => call.options.headers?.Authorization;
const refreshCalls = (calls) => calls.filter((call) => call.url === "/api/v1/auth/refresh");

describe("request", () => {
  it("sends the access token and parses JSON", async () => {
    const { fetchImpl, calls } = fakeFetch(() => json(200, [{ id: "a1" }]));
    const client = createApiClient({ fetchImpl });
    client.setAccessToken("token-1");

    const body = await client.request("/accounts");

    expect(body).toEqual([{ id: "a1" }]);
    expect(calls[0].url).toBe("/api/v1/accounts");
    expect(bearer(calls[0])).toBe("Bearer token-1");
  });

  it("sends a plain object as JSON", async () => {
    const { fetchImpl, calls } = fakeFetch(() => json(201, { id: "a1" }));
    const client = createApiClient({ fetchImpl });

    await client.request("/accounts", { method: "POST", body: { nickname: "Salary" } });

    expect(calls[0].options.method).toBe("POST");
    expect(calls[0].options.headers["Content-Type"]).toBe("application/json");
    expect(calls[0].options.body).toBe('{"nickname":"Salary"}');
  });

  it("sends FormData as it is, letting fetch set the multipart Content-Type", async () => {
    const { fetchImpl, calls } = fakeFetch(() => json(202, { id: "u1" }));
    const client = createApiClient({ fetchImpl });
    const form = new FormData();
    form.append("account_id", "a1");

    await client.request("/uploads", { method: "POST", body: form });

    expect(calls[0].options.body).toBe(form);
    expect(calls[0].options.headers["Content-Type"]).toBeUndefined();
  });

  it("answers null for a 204 No Content", async () => {
    const { fetchImpl } = fakeFetch(() => new Response(null, { status: 204 }));
    const client = createApiClient({ fetchImpl });

    expect(await client.request("/accounts/a1", { method: "DELETE" })).toBeNull();
  });

  it("turns the API's error shape into an ApiError", async () => {
    const { fetchImpl } = fakeFetch(() =>
      json(
        429,
        {
          error: {
            code: "RATE_LIMITED",
            message: "Too many requests.",
            details: null,
          },
        },
        { "Retry-After": "42" },
      ),
    );
    const client = createApiClient({ fetchImpl });

    const error = await client.request("/auth/login", { method: "POST" }).catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 429,
      code: "RATE_LIMITED",
      message: "Too many requests.",
      retryAfterSeconds: 42,
    });
  });

  it("gives a generic ApiError for an answer that is not JSON", async () => {
    const { fetchImpl } = fakeFetch(() => new Response("Bad gateway", { status: 502 }));
    const client = createApiClient({ fetchImpl });

    const error = await client.request("/accounts").catch((e) => e);

    expect(error).toMatchObject({ status: 502, code: "HTTP_ERROR", retryAfterSeconds: null });
  });
});

describe("an expired access token", () => {
  it("is refreshed once and the request repeated with the new token", async () => {
    const { fetchImpl, calls } = fakeFetch((url, options) => {
      if (url === "/api/v1/auth/refresh") {
        return json(200, { access_token: "token-2", token_type: "bearer", expires_in: 900 });
      }
      return options.headers.Authorization === "Bearer token-2"
        ? json(200, { ok: true })
        : unauthorized();
    });
    const client = createApiClient({ fetchImpl });
    client.setAccessToken("token-1");

    const body = await client.request("/accounts");

    expect(body).toEqual({ ok: true });
    expect(calls.map((call) => call.url)).toEqual([
      "/api/v1/accounts",
      "/api/v1/auth/refresh",
      "/api/v1/accounts",
    ]);
    expect(bearer(calls[2])).toBe("Bearer token-2");
    // The refresh itself carries no bearer token: the cookie is the credential.
    expect(bearer(calls[1])).toBeUndefined();
    expect(calls[1].options.credentials).toBe("same-origin");
    expect(client.getAccessToken()).toBe("token-2");
  });

  it("three 401s at the same moment cause ONE refresh", async () => {
    // A refresh that answers only when the test releases it, so all three
    // requests are certainly waiting for it at the same time.
    let releaseRefresh;
    const refreshAnswer = new Promise((resolve) => {
      releaseRefresh = () => resolve(json(200, { access_token: "token-2", expires_in: 900 }));
    });
    const { fetchImpl, calls } = fakeFetch((url, options) => {
      if (url === "/api/v1/auth/refresh") {
        return refreshAnswer;
      }
      return options.headers.Authorization === "Bearer token-2"
        ? json(200, { url })
        : unauthorized();
    });
    const client = createApiClient({ fetchImpl });
    client.setAccessToken("token-1");

    const results = Promise.all([
      client.request("/accounts"),
      client.request("/uploads"),
      client.request("/transactions"),
    ]);
    // Let the three first attempts fail and reach the refresh.
    await vi.waitFor(() => expect(refreshCalls(calls)).toHaveLength(1));
    releaseRefresh();

    expect(await results).toEqual([
      { url: "/api/v1/accounts" },
      { url: "/api/v1/uploads" },
      { url: "/api/v1/transactions" },
    ]);
    // Two refreshes with the same cookie would look like a stolen token and
    // end the session (ADR 005).
    expect(refreshCalls(calls)).toHaveLength(1);
  });

  it("a 401 that arrives after another call refreshed just retries", async () => {
    // The other call already got token-2 while this one was on its way with
    // token-1: no second refresh is needed.
    const { fetchImpl, calls } = fakeFetch((url, options) => {
      if (options.headers.Authorization === "Bearer token-2") {
        return json(200, { ok: true });
      }
      client.setAccessToken("token-2"); // the other call's refresh lands meanwhile
      return unauthorized();
    });
    const client = createApiClient({ fetchImpl });
    client.setAccessToken("token-1");

    expect(await client.request("/accounts")).toEqual({ ok: true });
    expect(refreshCalls(calls)).toHaveLength(0);
  });

  it("a refused refresh ends the session", async () => {
    const { fetchImpl, calls } = fakeFetch(() => unauthorized());
    const client = createApiClient({ fetchImpl });
    client.setAccessToken("token-1");
    const listener = vi.fn();
    client.subscribe(listener);

    const error = await client.request("/accounts").catch((e) => e);

    expect(error).toMatchObject({ status: 401, code: "UNAUTHORIZED" });
    expect(client.getAccessToken()).toBeNull();
    expect(listener).toHaveBeenCalled(); // the auth context hears about it
    // Tried once, refreshed once, and NOT retried: no loop.
    expect(calls.map((call) => call.url)).toEqual(["/api/v1/accounts", "/api/v1/auth/refresh"]);
  });

  it("is retried only once, even if the new token is refused too", async () => {
    const { fetchImpl, calls } = fakeFetch((url) =>
      url === "/api/v1/auth/refresh" ? json(200, { access_token: "token-2" }) : unauthorized(),
    );
    const client = createApiClient({ fetchImpl });
    client.setAccessToken("token-1");

    const error = await client.request("/accounts").catch((e) => e);

    expect(error.status).toBe(401);
    expect(calls).toHaveLength(3);
  });

  it("a refresh that fails for another reason keeps the session", async () => {
    // A 500 says nothing about the session; logging the user out for a
    // server hiccup would be wrong.
    const { fetchImpl } = fakeFetch((url) =>
      url === "/api/v1/auth/refresh"
        ? json(500, { error: { code: "INTERNAL_ERROR", message: "Something went wrong." } })
        : unauthorized(),
    );
    const client = createApiClient({ fetchImpl });
    client.setAccessToken("token-1");

    const error = await client.request("/accounts").catch((e) => e);

    expect(error).toMatchObject({ status: 500, code: "INTERNAL_ERROR" });
    expect(client.getAccessToken()).toBe("token-1");
  });

  it("is never refreshed for the auth routes themselves", async () => {
    // A 401 from /auth/login means a wrong password, not an expired token.
    const { fetchImpl, calls } = fakeFetch(() =>
      json(401, { error: { code: "UNAUTHORIZED", message: "Invalid email or password." } }),
    );
    const client = createApiClient({ fetchImpl });

    const error = await client.request("/auth/login", { method: "POST" }).catch((e) => e);

    expect(error.message).toBe("Invalid email or password.");
    expect(calls).toHaveLength(1);
  });
});

describe("subscribe", () => {
  it("stops calling a listener after it unsubscribes", () => {
    const client = createApiClient({ fetchImpl: vi.fn() });
    const listener = vi.fn();
    const unsubscribe = client.subscribe(listener);

    client.setAccessToken("token-1");
    unsubscribe();
    client.setAccessToken(null);

    expect(listener).toHaveBeenCalledTimes(1);
  });
});
