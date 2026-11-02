import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LoginForm } from "@/components/login-form";
import { apiError, json, renderWithSession, sentBody } from "@/test/session";

async function renderForm(routes) {
  const rendered = renderWithSession(<LoginForm />, { routes });
  // Wait for the session restore on page load to finish (anonymous here).
  await waitFor(() => expect(rendered.fetchImpl).toHaveBeenCalled());
  return rendered;
}

function fillAndSubmit(email = "alice@example.com", password = "correct horse battery") {
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: email } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: password } });
  fireEvent.click(screen.getByRole("button", { name: "Log in" }));
}

describe("LoginForm", () => {
  it("logs in with what was typed", async () => {
    const { client, fetchImpl } = await renderForm({
      "/auth/login": () => json(200, { access_token: "token-9", expires_in: 900 }),
    });

    fillAndSubmit();

    await waitFor(() => expect(client.getAccessToken()).toBe("token-9"));
    expect(sentBody(fetchImpl, "/auth/login")).toEqual({
      email: "alice@example.com",
      password: "correct horse battery",
    });
  });

  it("shows the backend's generic message for wrong details", async () => {
    await renderForm({
      "/auth/login": () => apiError(401, "UNAUTHORIZED", "Invalid email or password."),
    });

    fillAndSubmit("alice@example.com", "wrong password");

    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid email or password.");
    // The form can be sent again.
    expect(screen.getByRole("button", { name: "Log in" })).toBeEnabled();
  });

  it("says how long to wait after too many attempts", async () => {
    await renderForm({
      "/auth/login": () =>
        apiError(429, "RATE_LIMITED", "Too many requests.", { headers: { "Retry-After": "37" } }),
    });

    fillAndSubmit();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Too many attempts. Try again in 37 seconds.",
    );
  });

  it("shows a validation error under its field", async () => {
    await renderForm({
      "/auth/login": () =>
        apiError(422, "VALIDATION_ERROR", "The request contains invalid data.", {
          details: [
            { field: "body.email", message: "value is not a valid email address", type: "x" },
          ],
        }),
    });

    fillAndSubmit("alice@example", "correct horse battery");

    expect(await screen.findByText("value is not a valid email address")).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toHaveAttribute("aria-invalid", "true");
  });

  it("sends one login however fast the form is submitted twice", async () => {
    // A double click or Enter pressed twice: both submits arrive before
    // React has re-rendered the button as disabled.
    const { fetchImpl } = await renderForm({ "/auth/login": () => new Promise(() => {}) });
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "a@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "long enough" } });

    const form = screen.getByRole("button", { name: "Log in" }).closest("form");
    fireEvent.submit(form);
    fireEvent.submit(form);

    const logins = fetchImpl.mock.calls.filter(([url]) => url === "/api/v1/auth/login");
    expect(logins).toHaveLength(1);
  });

  it("can be sent again after an error", async () => {
    const { fetchImpl } = await renderForm({
      "/auth/login": () => apiError(401, "UNAUTHORIZED", "Invalid email or password."),
    });

    fillAndSubmit();
    await screen.findByRole("alert");
    fillAndSubmit();

    await waitFor(() => {
      const logins = fetchImpl.mock.calls.filter(([url]) => url === "/api/v1/auth/login");
      expect(logins).toHaveLength(2);
    });
  });

  it("disables the button while logging in", async () => {
    // A login that never answers, so the "in progress" state can be seen.
    await renderForm({ "/auth/login": () => new Promise(() => {}) });

    fillAndSubmit();

    expect(await screen.findByRole("button", { name: "Logging in…" })).toBeDisabled();
  });
});
