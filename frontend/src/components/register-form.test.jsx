import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RegisterForm } from "@/components/register-form";
import { apiError, json, renderWithSession, sentBody } from "@/test/session";

const NEW_USER = { name: "Alice", email: "alice@example.com", password: "correct horse battery" };

const created = () =>
  json(201, { id: "11111111-1111-4111-8111-111111111111", email: NEW_USER.email, name: "Alice" });
const loggedIn = () => json(200, { access_token: "token-9", expires_in: 900 });

async function renderForm(routes) {
  const rendered = renderWithSession(<RegisterForm />, { routes });
  await waitFor(() => expect(rendered.fetchImpl).toHaveBeenCalled());
  return rendered;
}

function fillAndSubmit(user = NEW_USER) {
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: user.name } });
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: user.email } });
  fireEvent.change(screen.getByLabelText(/^Password/), { target: { value: user.password } });
  fireEvent.click(screen.getByRole("button", { name: "Create account" }));
}

describe("RegisterForm", () => {
  it("creates the account, then logs in with the same details", async () => {
    const { client, fetchImpl } = await renderForm({
      "/auth/register": created,
      "/auth/login": loggedIn,
    });

    fillAndSubmit();

    await waitFor(() => expect(client.getAccessToken()).toBe("token-9"));
    expect(sentBody(fetchImpl, "/auth/register")).toEqual(NEW_USER);
    expect(sentBody(fetchImpl, "/auth/login")).toEqual({
      email: NEW_USER.email,
      password: NEW_USER.password,
    });
  });

  it("says when the email is already registered, and doesn't try to log in", async () => {
    const { fetchImpl } = await renderForm({
      "/auth/register": () =>
        apiError(409, "CONFLICT", "An account with this email already exists."),
      "/auth/login": loggedIn,
    });

    fillAndSubmit();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "An account with this email already exists.",
    );
    expect(sentBody(fetchImpl, "/auth/login")).toBeUndefined();
  });

  it("shows the backend's password rule under the password field", async () => {
    await renderForm({
      "/auth/register": () =>
        apiError(422, "VALIDATION_ERROR", "The request contains invalid data.", {
          details: [
            {
              field: "body.password",
              message: "String should have at least 8 characters",
              type: "string_too_short",
            },
          ],
        }),
    });

    fillAndSubmit({ ...NEW_USER, password: "short" });

    expect(await screen.findByText("String should have at least 8 characters")).toBeInTheDocument();
  });

  it("tells the user to log in if only the automatic login failed", async () => {
    await renderForm({
      "/auth/register": created,
      "/auth/login": () =>
        apiError(429, "RATE_LIMITED", "Too many requests.", { headers: { "Retry-After": "20" } }),
    });

    fillAndSubmit();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Your account was created, but logging in failed: Too many attempts. Try again in 20 seconds. Please log in.",
    );
  });

  it("registers once however fast the form is submitted twice", async () => {
    // A second POST /auth/register would answer 409 "already exists" for
    // the account the first one just created.
    let finishRegister;
    const { fetchImpl } = await renderForm({
      "/auth/register": () =>
        new Promise((resolve) => {
          finishRegister = () => resolve(created());
        }),
      "/auth/login": loggedIn,
    });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: NEW_USER.name } });
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: NEW_USER.email } });
    fireEvent.change(screen.getByLabelText(/^Password/), {
      target: { value: NEW_USER.password },
    });

    const form = screen.getByRole("button", { name: "Create account" }).closest("form");
    fireEvent.submit(form);
    fireEvent.submit(form);
    finishRegister();

    await waitFor(() => expect(sentBody(fetchImpl, "/auth/login")).toBeDefined());
    const registers = fetchImpl.mock.calls.filter(([url]) => url === "/api/v1/auth/register");
    expect(registers).toHaveLength(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("checks the password length in the browser before sending", () => {
    renderWithSession(<RegisterForm />);

    expect(screen.getByLabelText(/^Password/)).toHaveAttribute("minLength", "8");
    expect(screen.getByLabelText(/^Password/)).toHaveAttribute("autoComplete", "new-password");
  });
});
