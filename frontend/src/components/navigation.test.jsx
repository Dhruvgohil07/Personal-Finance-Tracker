// The components that navigate: the guard (RequireAuth), the redirect on the
// login pages (RedirectIfSignedIn) and the header's logout.
//
// next/navigation only works inside a running Next.js app, so it is
// replaced with a fake router whose calls the tests can check.

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AppHeader } from "@/components/app-header";
import { RedirectIfSignedIn } from "@/components/redirect-if-signed-in";
import { RequireAuth } from "@/components/require-auth";
import { json, renderWithSession } from "@/test/session";

// vi.hoisted: runs before the vi.mock below, which Vitest moves to the top
// of the file, so the mock can use these values.
const navigation = vi.hoisted(() => ({
  replace: vi.fn(),
  pathname: "/transactions",
  search: "",
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: navigation.replace, push: vi.fn() }),
  usePathname: () => navigation.pathname,
  useSearchParams: () => new URLSearchParams(navigation.search),
}));

beforeEach(() => {
  navigation.replace.mockReset();
  navigation.pathname = "/transactions";
  navigation.search = "";
});

describe("RequireAuth", () => {
  it("shows the page to a signed-in user", async () => {
    renderWithSession(<RequireAuth>secret page</RequireAuth>, { signedIn: true });

    expect(await screen.findByText("secret page")).toBeInTheDocument();
    expect(navigation.replace).not.toHaveBeenCalled();
  });

  it("shows nothing of the page while the session is being checked", () => {
    renderWithSession(<RequireAuth>secret page</RequireAuth>, { signedIn: true });

    expect(screen.getByRole("status")).toHaveTextContent("Checking your session…");
    expect(screen.queryByText("secret page")).not.toBeInTheDocument();
  });

  it("sends a logged-out visitor to the login page, remembering where they were", async () => {
    renderWithSession(<RequireAuth>secret page</RequireAuth>, { signedIn: false });

    await waitFor(() =>
      expect(navigation.replace).toHaveBeenCalledWith("/login?next=%2Ftransactions"),
    );
    expect(screen.queryByText("secret page")).not.toBeInTheDocument();
  });
});

describe("RedirectIfSignedIn", () => {
  it("takes a signed-in user to the page in ?next=", async () => {
    navigation.search = "next=%2Ftransactions";
    renderWithSession(<RedirectIfSignedIn />, { signedIn: true });

    await waitFor(() => expect(navigation.replace).toHaveBeenCalledWith("/transactions"));
  });

  it("never follows ?next= to another site", async () => {
    navigation.search = "next=%2F%2Fevil.example";
    renderWithSession(<RedirectIfSignedIn />, { signedIn: true });

    await waitFor(() => expect(navigation.replace).toHaveBeenCalledWith("/"));
  });

  it("leaves a logged-out visitor on the login page", async () => {
    const { fetchImpl } = renderWithSession(<RedirectIfSignedIn />, { signedIn: false });

    await waitFor(() => expect(fetchImpl).toHaveBeenCalled());
    expect(navigation.replace).not.toHaveBeenCalled();
  });
});

describe("AppHeader", () => {
  const me = () =>
    json(200, {
      id: "11111111-1111-4111-8111-111111111111",
      email: "alice@example.com",
      name: "Alice",
      settings: { ai_enabled: true, weekly_email: false },
    });

  it("shows the user's name from GET /me", async () => {
    renderWithSession(<AppHeader />, { signedIn: true, routes: { "/me": me } });

    expect(await screen.findByText("Alice")).toBeInTheDocument();
  });

  it("logs out: the server is told and the session ends", async () => {
    const { client, fetchImpl } = renderWithSession(
      <RequireAuth>
        <AppHeader />
      </RequireAuth>,
      {
        signedIn: true,
        routes: { "/me": me, "/auth/logout": () => new Response(null, { status: 204 }) },
      },
    );
    await screen.findByText("Alice");

    fireEvent.click(screen.getByRole("button", { name: "Log out" }));

    // The guard, not the header, then sends the user to the login page.
    await waitFor(() =>
      expect(navigation.replace).toHaveBeenCalledWith("/login?next=%2Ftransactions"),
    );
    expect(client.getAccessToken()).toBeNull();
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/v1/auth/logout",
      expect.objectContaining({ method: "POST" }),
    );
  });
});
