import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api";
import { AuthProvider, useAuth } from "@/lib/auth";

function json(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const unauthorized = () => json(401, { error: { code: "UNAUTHORIZED", message: "No." } });

/** Render the provider around a component that shows the session status. */
function renderSession(handler) {
  const fetchImpl = vi.fn(async (url, options) => handler(url, options));
  const client = createApiClient({ fetchImpl });
  const queryClient = new QueryClient();
  let auth;

  function Probe() {
    auth = useAuth();
    return <p data-testid="status">{auth.status}</p>;
  }

  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider client={client}>
        <Probe />
      </AuthProvider>
    </QueryClientProvider>,
  );
  // `auth` is read through a function so the test always sees the latest render.
  return { client, queryClient, fetchImpl, getAuth: () => auth };
}

const status = () => screen.getByTestId("status").textContent;

describe("AuthProvider", () => {
  it("restores the session from the refresh cookie on page load", async () => {
    renderSession((url) =>
      url === "/api/v1/auth/refresh" ? json(200, { access_token: "token-1" }) : unauthorized(),
    );

    expect(status()).toBe("loading");
    await waitFor(() => expect(status()).toBe("authenticated"));
  });

  it("is anonymous when there is no session to restore", async () => {
    renderSession(() => unauthorized());

    await waitFor(() => expect(status()).toBe("anonymous"));
  });

  it("is anonymous, not stuck loading, when the restore fails", async () => {
    renderSession(() => {
      throw new TypeError("Failed to fetch"); // what fetch throws when offline
    });

    await waitFor(() => expect(status()).toBe("anonymous"));
  });

  it("logs in: the token goes into the client, never into storage", async () => {
    const { client, getAuth } = renderSession((url) =>
      url === "/api/v1/auth/login"
        ? json(200, { access_token: "token-1", token_type: "bearer", expires_in: 900 })
        : unauthorized(),
    );
    await waitFor(() => expect(status()).toBe("anonymous"));

    await act(() => getAuth().login("alice@example.com", "correct horse battery"));

    expect(status()).toBe("authenticated");
    expect(client.getAccessToken()).toBe("token-1");
    // Hard constraint 1: the token is never persisted in the browser.
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
    expect(document.cookie).toBe("");
  });

  it("a failed login leaves the session anonymous and reports the error", async () => {
    const { getAuth } = renderSession((url) =>
      url === "/api/v1/auth/login"
        ? json(401, { error: { code: "UNAUTHORIZED", message: "Invalid email or password." } })
        : unauthorized(),
    );
    await waitFor(() => expect(status()).toBe("anonymous"));

    await expect(getAuth().login("alice@example.com", "wrong")).rejects.toThrow(
      "Invalid email or password.",
    );
    expect(status()).toBe("anonymous");
  });

  it("logs out, and forgets the cached API data of that session", async () => {
    const { fetchImpl, queryClient, getAuth } = renderSession((url) => {
      if (url === "/api/v1/auth/refresh") {
        return json(200, { access_token: "token-1" });
      }
      return new Response(null, { status: 204 }); // POST /auth/logout
    });
    await waitFor(() => expect(status()).toBe("authenticated"));
    queryClient.setQueryData(["accounts"], [{ id: "a1" }]);

    await act(() => getAuth().logout());

    expect(status()).toBe("anonymous");
    expect(fetchImpl).toHaveBeenCalledWith("/api/v1/auth/logout", expect.anything());
    expect(queryClient.getQueryData(["accounts"])).toBeUndefined();
  });

  it("logs out locally even if the logout request fails", async () => {
    const { getAuth } = renderSession((url) => {
      if (url === "/api/v1/auth/refresh") {
        return json(200, { access_token: "token-1" });
      }
      throw new TypeError("Failed to fetch");
    });
    await waitFor(() => expect(status()).toBe("authenticated"));

    await act(() => getAuth().logout().catch(() => {}));

    expect(status()).toBe("anonymous");
  });

  it("deletes the account: sends only the password, then ends the session", async () => {
    const { fetchImpl, queryClient, getAuth } = renderSession((url) => {
      if (url === "/api/v1/auth/refresh") {
        return json(200, { access_token: "token-1" });
      }
      return new Response(null, { status: 204 }); // DELETE /me
    });
    await waitFor(() => expect(status()).toBe("authenticated"));
    queryClient.setQueryData(["accounts"], [{ id: "a1" }]);

    await act(() => getAuth().deleteAccount("correct horse battery"));

    const [, options] = fetchImpl.mock.calls.find(([url]) => url === "/api/v1/me");
    expect(options.method).toBe("DELETE");
    expect(JSON.parse(options.body)).toEqual({ password: "correct horse battery" });
    expect(status()).toBe("anonymous");
    expect(getAuth().loggedOut).toBe(true);
    expect(getAuth().accountDeleted).toBe(true);
    expect(queryClient.getQueryData(["accounts"])).toBeUndefined();
  });

  it("keeps the session when the password is wrong", async () => {
    const { client, getAuth } = renderSession((url) => {
      if (url === "/api/v1/auth/refresh") {
        return json(200, { access_token: "token-1" });
      }
      return json(400, {
        error: { code: "INCORRECT_PASSWORD", message: "That password is not correct." },
      });
    });
    await waitFor(() => expect(status()).toBe("authenticated"));

    await expect(getAuth().deleteAccount("wrong")).rejects.toThrow("That password is not correct.");

    expect(status()).toBe("authenticated");
    expect(client.getAccessToken()).toBe("token-1");
    expect(getAuth().accountDeleted).toBe(false);
  });

  it("forgets the deletion at the next login", async () => {
    const { getAuth } = renderSession((url) => {
      if (url === "/api/v1/auth/refresh") {
        return json(200, { access_token: "token-1" });
      }
      if (url === "/api/v1/auth/login") {
        return json(200, { access_token: "token-2", token_type: "bearer", expires_in: 900 });
      }
      return new Response(null, { status: 204 });
    });
    await waitFor(() => expect(status()).toBe("authenticated"));
    await act(() => getAuth().deleteAccount("correct horse battery"));

    await act(() => getAuth().login("new@example.com", "another password"));

    expect(getAuth().accountDeleted).toBe(false);
    expect(getAuth().loggedOut).toBe(false);
  });

  it("a session the API ended clears the cache too", async () => {
    const { client, queryClient } = renderSession((url) =>
      url === "/api/v1/auth/refresh" ? json(200, { access_token: "token-1" }) : unauthorized(),
    );
    await waitFor(() => expect(status()).toBe("authenticated"));
    queryClient.setQueryData(["accounts"], [{ id: "a1" }]);

    // e.g. a request after the refresh cookie was revoked
    act(() => client.setAccessToken(null));

    expect(status()).toBe("anonymous");
    expect(queryClient.getQueryData(["accounts"])).toBeUndefined();
  });
});

describe("useAuth", () => {
  it("explains itself when used outside the provider", () => {
    // React logs the thrown error to the console; silence it for this test.
    vi.spyOn(console, "error").mockImplementation(() => {});

    expect(() => renderHook(() => useAuth())).toThrow("inside <AuthProvider>");
  });
});
