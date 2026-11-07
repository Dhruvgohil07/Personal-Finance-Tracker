import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { Activity, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LoginForm } from "@/components/login-form";
import { RequireAuth } from "@/components/require-auth";
import { SettingsView } from "@/components/settings/settings-view";
import { useAuth } from "@/lib/auth";
import { apiError, json, renderWithSession } from "@/test/session";

// next/navigation only works inside a running Next.js app, so the router
// is faked (as in navigation.test.jsx).
const navigation = vi.hoisted(() => ({ replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: navigation.replace }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/settings",
}));

beforeEach(() => {
  navigation.replace.mockReset();
});

const ME = {
  id: "u1",
  email: "asha@example.com",
  name: "Asha",
  settings: { ai_enabled: true, weekly_email: false },
};

const PASSWORD = "correct horse battery";

/**
 * The settings page as the signed-in layout shows it (inside the guard),
 * with the login form beside it once the session has ended: in the real
 * app the guard would navigate there.
 *
 * `deleteMe(body)` answers DELETE /me; by default it accepts PASSWORD only.
 */
function renderSettings({ deleteMe } = {}) {
  function LoginAfterwards() {
    return useAuth().status === "anonymous" ? <LoginForm /> : null;
  }

  return renderWithSession(
    <>
      <RequireAuth>
        <SettingsView />
      </RequireAuth>
      <LoginAfterwards />
    </>,
    {
      signedIn: true,
      handler: (path, options) => {
        if (path !== "/me") {
          return undefined;
        }
        if (options.method === "DELETE") {
          const body = JSON.parse(options.body);
          if (deleteMe) {
            return deleteMe(body);
          }
          return body.password === PASSWORD
            ? new Response(null, { status: 204 })
            : apiError(400, "INCORRECT_PASSWORD", "That password is not correct.");
        }
        return json(200, ME);
      },
    },
  );
}

async function openConfirmation() {
  fireEvent.click(await screen.findByRole("button", { name: "Delete my account…" }));
  return screen.getByRole("alertdialog", { name: "Delete your account?" });
}

function fill(label, value) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

const deleteButton = () => screen.getByRole("button", { name: "Delete my account forever" });

// The DELETE /me requests sent (GET /me goes to the same path).
const deleteRequests = (fetchImpl) =>
  fetchImpl.mock.calls.filter(
    ([url, options]) => url === "/api/v1/me" && options?.method === "DELETE",
  );

describe("Settings: profile", () => {
  it("shows the name and email from GET /me", async () => {
    renderSettings();

    const profile = await screen.findByRole("region", { name: "Your profile" });
    expect(await within(profile).findByText("Asha")).toBeInTheDocument();
    expect(within(profile).getByText("asha@example.com")).toBeInTheDocument();
  });
});

describe("Settings: delete account", () => {
  it("asks first, saying what is deleted", async () => {
    renderSettings();

    const dialog = await openConfirmation();

    expect(dialog).toHaveAccessibleDescription(/This permanently deletes/);
    expect(dialog).toHaveTextContent("It can't be undone, and you will be logged out.");
    // The first thing to fill in has the focus.
    expect(screen.getByLabelText("Type DELETE to confirm")).toHaveFocus();
  });

  it("can't be sent until DELETE and the password are typed", async () => {
    renderSettings();
    await openConfirmation();

    expect(deleteButton()).toBeDisabled();
    fill("Type DELETE to confirm", "delete");
    fill("Your password", PASSWORD);
    expect(deleteButton()).toBeDisabled(); // capitals, as asked

    fill("Type DELETE to confirm", "DELETE");
    expect(deleteButton()).toBeEnabled();

    fill("Your password", "");
    expect(deleteButton()).toBeDisabled();
  });

  it("lets the password manager fill in the login password", async () => {
    renderSettings();
    await openConfirmation();

    expect(screen.getByLabelText("Your password")).toHaveAttribute(
      "autocomplete",
      "current-password",
    );
    expect(screen.getByLabelText("Type DELETE to confirm")).toHaveAttribute("autocomplete", "off");
  });

  it("deletes, ends the session and says so on the login page", async () => {
    const { fetchImpl, client } = renderSettings();
    await openConfirmation();

    fill("Type DELETE to confirm", "DELETE");
    fill("Your password", PASSWORD);
    fireEvent.click(deleteButton());

    const notice = await screen.findByText("Your account and all its data have been deleted.");
    // Focused, so a screen reader reads it on the new page.
    expect(notice).toHaveFocus();
    // Only the password goes to the API; the typed word stays in the browser.
    const [[, options]] = deleteRequests(fetchImpl);
    expect(JSON.parse(options.body)).toEqual({ password: PASSWORD });
    expect(client.getAccessToken()).toBeNull();
    // The guard sends the browser to plain /login: nothing about the page.
    await waitFor(() => expect(navigation.replace).toHaveBeenCalledWith("/login"));
  });

  it("keeps you signed in after a wrong password, and lets you try again", async () => {
    const { client } = renderSettings();
    await openConfirmation();

    fill("Type DELETE to confirm", "DELETE");
    fill("Your password", "not my password");
    fireEvent.click(deleteButton());

    expect(await screen.findByText("That password is not correct.")).toBeInTheDocument();
    expect(screen.getByLabelText("Your password")).toHaveValue("");
    expect(screen.getByLabelText("Your password")).toHaveAttribute("aria-invalid", "true");
    expect(client.getAccessToken()).toBe("token-1");
    expect(navigation.replace).not.toHaveBeenCalled();

    fill("Your password", PASSWORD);
    fireEvent.click(deleteButton());
    expect(
      await screen.findByText("Your account and all its data have been deleted."),
    ).toBeInTheDocument();
  });

  it("says how long to wait after too many tries", async () => {
    renderSettings({
      deleteMe: () =>
        apiError(429, "RATE_LIMITED", "Too many requests.", { headers: { "Retry-After": "42" } }),
    });
    await openConfirmation();

    fill("Type DELETE to confirm", "DELETE");
    fill("Your password", PASSWORD);
    fireEvent.click(deleteButton());

    expect(await screen.findByRole("alert")).toHaveTextContent("Try again in 42 seconds.");
    expect(deleteButton()).toBeEnabled();
  });

  it("sends one request when submitted twice at once", async () => {
    const { fetchImpl } = renderSettings();
    const dialog = await openConfirmation();

    fill("Type DELETE to confirm", "DELETE");
    fill("Your password", PASSWORD);
    // Two submits in the same instant (a double Enter), before the
    // disabled button could show: the in-flight guard stops the second.
    fireEvent.submit(dialog);
    fireEvent.submit(dialog);

    await screen.findByText("Your account and all its data have been deleted.");
    expect(deleteRequests(fetchImpl)).toHaveLength(1);
  });

  it("closes the question when the page is left, forgetting the password", async () => {
    // With cacheComponents, Next.js hides a page that was left in React's
    // <Activity mode="hidden"> instead of unmounting it.
    // The mode is state inside the tree, so the providers that
    // renderWithSession put around it stay (a rerender would drop them).
    let setMode;
    function Page() {
      const [mode, set] = useState("visible");
      setMode = set;
      return (
        <Activity mode={mode}>
          <SettingsView />
        </Activity>
      );
    }
    renderWithSession(<Page />, {
      signedIn: true,
      handler: (path) => (path === "/me" ? json(200, ME) : undefined),
    });
    await openConfirmation();
    fill("Type DELETE to confirm", "DELETE");
    fill("Your password", PASSWORD);

    act(() => setMode("hidden"));
    act(() => setMode("visible"));

    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Your password")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete my account…" })).toBeInTheDocument();
  });

  it("closes the question on Cancel without deleting anything", async () => {
    const { fetchImpl } = renderSettings();
    await openConfirmation();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete my account…" })).toBeInTheDocument();
    expect(deleteRequests(fetchImpl)).toHaveLength(0);
  });
});
