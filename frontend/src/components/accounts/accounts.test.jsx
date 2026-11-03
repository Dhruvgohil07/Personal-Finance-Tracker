import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AccountsView } from "@/components/accounts/accounts-view";
import { apiError, json, renderWithSession } from "@/test/session";

import HomePage from "@/app/(app)/page";

const SALARY = {
  id: "11111111-1111-4111-8111-111111111111",
  bank_code: "ICICI",
  nickname: "Salary account",
  account_type: "savings",
  masked_number: "4321",
  created_at: "2026-11-01T10:00:00Z",
};

/**
 * A pretend accounts API, kept in memory like the real one: GET/POST
 * /accounts, PATCH/DELETE /accounts/<id>, with the backend's 409 for the
 * same bank + type + last 4 digits.
 */
function fakeAccountsApi(initial = []) {
  // Copies of each account: a PATCH changes the copy, never the shared
  // SALARY object that later tests start from.
  const state = { accounts: initial.map((account) => ({ ...account })), next: 2 };

  function handler(path, options) {
    const method = options.method ?? "GET";
    if (path === "/accounts" && method === "GET") {
      return json(200, state.accounts);
    }
    if (path === "/accounts" && method === "POST") {
      const body = JSON.parse(options.body);
      const duplicate = state.accounts.some(
        (a) =>
          a.bank_code === body.bank_code &&
          a.account_type === body.account_type &&
          a.masked_number === body.masked_number,
      );
      if (duplicate) {
        return apiError(409, "CONFLICT", "You have already added this account.");
      }
      const account = {
        id: `00000000-0000-4000-8000-00000000000${state.next++}`,
        created_at: "2026-11-03T10:00:00Z",
        ...body,
      };
      state.accounts.push(account);
      return json(201, account);
    }
    const match = path.match(/^\/accounts\/([^/]+)$/);
    if (match && method === "PATCH") {
      const account = state.accounts.find((a) => a.id === match[1]);
      Object.assign(account, JSON.parse(options.body));
      return json(200, account);
    }
    if (match && method === "DELETE") {
      state.accounts = state.accounts.filter((a) => a.id !== match[1]);
      return new Response(null, { status: 204 });
    }
    return undefined;
  }
  return { state, handler };
}

const requestsTo = (fetchImpl, method, path) =>
  fetchImpl.mock.calls.filter(
    ([url, options]) => url === `/api/v1${path}` && (options?.method ?? "GET") === method,
  );

function fillAccountForm({
  bank = "ICICI",
  nickname = "Rent account",
  type = "current",
  lastFour = "9876",
} = {}) {
  fireEvent.change(screen.getByLabelText("Bank"), { target: { value: bank } });
  fireEvent.change(screen.getByLabelText("Nickname"), { target: { value: nickname } });
  fireEvent.change(screen.getByLabelText("Account type"), { target: { value: type } });
  fireEvent.change(screen.getByLabelText("Last 4 digits of the account number"), {
    target: { value: lastFour },
  });
}

describe("AccountsView: the list", () => {
  it("shows each account with readable labels and only its last 4 digits", async () => {
    renderWithSession(<AccountsView />, {
      signedIn: true,
      handler: fakeAccountsApi([SALARY]).handler,
    });

    const row = (await screen.findByText("Salary account")).closest("li");
    expect(row).toHaveTextContent("ICICI Bank · Savings · •••• 4321");
  });

  it("says so when there are no accounts yet", async () => {
    renderWithSession(<AccountsView />, { signedIn: true, handler: fakeAccountsApi().handler });

    expect(await screen.findByText(/No accounts yet/)).toBeInTheDocument();
  });
});

describe("AccountsView: adding", () => {
  it("adds an account and shows it in the list", async () => {
    const api = fakeAccountsApi([SALARY]);
    const { fetchImpl } = renderWithSession(<AccountsView />, {
      signedIn: true,
      handler: api.handler,
    });
    await screen.findByText("Salary account");

    fillAccountForm({ bank: "HDFC", nickname: "  Rent account  " });
    fireEvent.click(screen.getByRole("button", { name: "Add account" }));

    expect(await screen.findByText("Rent account")).toBeInTheDocument();
    const [[, options]] = requestsTo(fetchImpl, "POST", "/accounts");
    expect(JSON.parse(options.body)).toEqual({
      bank_code: "HDFC",
      nickname: "Rent account", // trimmed
      account_type: "current",
      masked_number: "9876",
    });
    // The form is empty again, ready for the next one.
    expect(screen.getByLabelText("Nickname")).toHaveValue("");
  });

  it.each([["123456789012"], ["12a4"], ["123"], [""]])(
    "never sends %j: only exactly 4 digits leave the browser",
    async (lastFour) => {
      const { fetchImpl } = renderWithSession(<AccountsView />, {
        signedIn: true,
        handler: fakeAccountsApi().handler,
      });
      await screen.findByText(/No accounts yet/);

      fillAccountForm({ lastFour });
      // submit() on the form: like a script would, skipping the browser's own
      // `pattern` check, which is exactly why the code checks again.
      fireEvent.submit(screen.getByRole("button", { name: "Add account" }).closest("form"));

      expect(
        await screen.findByText("Enter exactly the last 4 digits, nothing more."),
      ).toBeInTheDocument();
      expect(requestsTo(fetchImpl, "POST", "/accounts")).toHaveLength(0);
    },
  );

  it("explains an account that was already added (409)", async () => {
    renderWithSession(<AccountsView />, {
      signedIn: true,
      handler: fakeAccountsApi([SALARY]).handler,
    });
    await screen.findByText("Salary account");

    fillAccountForm({ bank: "ICICI", type: "savings", lastFour: "4321" });
    fireEvent.click(screen.getByRole("button", { name: "Add account" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You have already added this account.",
    );
  });

  it("shows a validation error under its field", async () => {
    renderWithSession(<AccountsView />, {
      signedIn: true,
      handler: (path, options) =>
        path === "/accounts" && options.method === "POST"
          ? apiError(422, "VALIDATION_ERROR", "The request contains invalid data.", {
              details: [
                { field: "body.nickname", message: "String should have at most 50 characters" },
              ],
            })
          : fakeAccountsApi().handler(path, options),
    });
    await screen.findByText(/No accounts yet/);

    fillAccountForm();
    fireEvent.click(screen.getByRole("button", { name: "Add account" }));

    expect(await screen.findByText("String should have at most 50 characters")).toBeInTheDocument();
    expect(screen.getByLabelText("Nickname")).toHaveAttribute("aria-invalid", "true");
  });

  it("adds once however fast the form is submitted twice", async () => {
    const { fetchImpl } = renderWithSession(<AccountsView />, {
      signedIn: true,
      handler: fakeAccountsApi().handler,
    });
    await screen.findByText(/No accounts yet/);

    fillAccountForm();
    const form = screen.getByRole("button", { name: "Add account" }).closest("form");
    fireEvent.submit(form);
    fireEvent.submit(form);

    await screen.findByText("Rent account");
    expect(requestsTo(fetchImpl, "POST", "/accounts")).toHaveLength(1);
  });
});

describe("AccountsView: editing", () => {
  it("renames an account and changes its type", async () => {
    const api = fakeAccountsApi([SALARY]);
    const { fetchImpl } = renderWithSession(<AccountsView />, {
      signedIn: true,
      handler: api.handler,
    });
    const row = (await screen.findByText("Salary account")).closest("li");

    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
    fireEvent.change(within(row).getByLabelText("Nickname"), { target: { value: "Main account" } });
    fireEvent.change(within(row).getByLabelText("Account type"), { target: { value: "current" } });
    fireEvent.click(within(row).getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Main account")).toBeInTheDocument();
    const [[, options]] = requestsTo(fetchImpl, "PATCH", `/accounts/${SALARY.id}`);
    expect(JSON.parse(options.body)).toEqual({ nickname: "Main account", account_type: "current" });
  });

  it("offers no way to change the bank or the last 4 digits", async () => {
    renderWithSession(<AccountsView />, {
      signedIn: true,
      handler: fakeAccountsApi([SALARY]).handler,
    });
    const row = (await screen.findByText("Salary account")).closest("li");

    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));

    expect(within(row).queryByLabelText("Bank")).not.toBeInTheDocument();
    expect(within(row).queryByLabelText(/Last 4 digits/)).not.toBeInTheDocument();
  });
});

describe("AccountsView: deleting", () => {
  it("asks first, warning that statements and transactions go too", async () => {
    const { fetchImpl } = renderWithSession(<AccountsView />, {
      signedIn: true,
      handler: fakeAccountsApi([SALARY]).handler,
    });
    const row = (await screen.findByText("Salary account")).closest("li");

    fireEvent.click(within(row).getByRole("button", { name: "Delete" }));

    const confirm = within(row).getByRole("group", { name: "Delete Salary account" });
    expect(confirm).toHaveTextContent("permanently deletes all its statements and transactions");
    // A stray Enter presses Cancel, not Delete.
    expect(within(confirm).getByRole("button", { name: "Cancel" })).toHaveFocus();
    expect(requestsTo(fetchImpl, "DELETE", `/accounts/${SALARY.id}`)).toHaveLength(0);
  });

  it("keeps the account when the user cancels", async () => {
    const { fetchImpl } = renderWithSession(<AccountsView />, {
      signedIn: true,
      handler: fakeAccountsApi([SALARY]).handler,
    });
    const row = (await screen.findByText("Salary account")).closest("li");

    fireEvent.click(within(row).getByRole("button", { name: "Delete" }));
    fireEvent.click(within(row).getByRole("button", { name: "Cancel" }));

    expect(within(row).queryByRole("group")).not.toBeInTheDocument();
    expect(requestsTo(fetchImpl, "DELETE", `/accounts/${SALARY.id}`)).toHaveLength(0);
  });

  it("deletes the account after confirming", async () => {
    const { fetchImpl } = renderWithSession(<AccountsView />, {
      signedIn: true,
      handler: fakeAccountsApi([SALARY]).handler,
    });
    const row = (await screen.findByText("Salary account")).closest("li");

    fireEvent.click(within(row).getByRole("button", { name: "Delete" }));
    fireEvent.click(within(row).getByRole("button", { name: "Delete account" }));

    expect(await screen.findByText(/No accounts yet/)).toBeInTheDocument();
    expect(requestsTo(fetchImpl, "DELETE", `/accounts/${SALARY.id}`)).toHaveLength(1);
  });
});

describe("HomePage: first run", () => {
  it("asks a new user for their first account, then welcomes them", async () => {
    renderWithSession(<HomePage />, {
      signedIn: true,
      handler: fakeAccountsApi().handler,
      routes: {
        "/me": () =>
          json(200, {
            id: "u1",
            email: "a@example.com",
            name: "Asha",
            settings: { ai_enabled: true, weekly_email: false },
          }),
      },
    });

    expect(await screen.findByText("Add your first account")).toBeInTheDocument();

    fillAccountForm();
    fireEvent.click(screen.getByRole("button", { name: "Add my first account" }));

    expect(await screen.findByText(/You have 1 account\./)).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Welcome, Asha");
    expect(screen.queryByText("Add your first account")).not.toBeInTheDocument();
  });

  it("goes straight to the welcome when there already are accounts", async () => {
    renderWithSession(<HomePage />, {
      signedIn: true,
      handler: fakeAccountsApi([SALARY]).handler,
    });

    expect(await screen.findByText(/You have 1 account\./)).toBeInTheDocument();
  });
});
