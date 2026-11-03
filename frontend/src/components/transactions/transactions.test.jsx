import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TransactionsView } from "@/components/transactions/transactions-view";
import { apiError, json, renderWithSession } from "@/test/session";

// next/navigation only works inside a running Next.js app, so the router
// and the URL's query are faked (as in navigation.test.jsx).
const navigation = vi.hoisted(() => ({ push: vi.fn(), search: "" }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: navigation.push, replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(navigation.search),
  usePathname: () => "/transactions",
}));

beforeEach(() => {
  navigation.push.mockReset();
  navigation.search = "";
});

afterEach(() => {
  vi.useRealTimers();
});

const ACCOUNT = {
  id: "11111111-1111-4111-8111-111111111111",
  bank_code: "ICICI",
  nickname: "Salary account",
  account_type: "savings",
  masked_number: "4321",
  created_at: "2026-11-01T10:00:00Z",
};

const CATEGORIES = [
  { id: "c0000000-0000-4000-8000-000000000001", name: "Groceries", slug: "groceries", kind: "expense", parent_id: null, icon: null, color: null, is_system: true },
  { id: "c0000000-0000-4000-8000-000000000002", name: "Salary", slug: "salary", kind: "income", parent_id: null, icon: null, color: null, is_system: true },
  { id: "c0000000-0000-4000-8000-000000000003", name: "Self transfer", slug: "self-transfer", kind: "transfer", parent_id: null, icon: null, color: null, is_system: true },
];
const [GROCERIES, SALARY] = CATEGORIES;

let nextId = 1;
// A transaction as the API returns it; `overrides` changes what a test needs.
function aTransaction(overrides = {}) {
  const id = `t0000000-0000-4000-8000-${String(nextId++).padStart(12, "0")}`;
  return {
    id,
    account_id: ACCOUNT.id,
    upload_id: "u0000000-0000-4000-8000-000000000001",
    txn_date: "2026-08-05",
    value_date: null,
    amount_paise: 7000,
    direction: "debit",
    balance_after_paise: null,
    raw_description: "UPI/MOONCAFE/PAY",
    normalized_description: "MOONCAFE",
    channel: "upi",
    counterparty_vpa: null,
    merchant_key: "MOONCAFE",
    // What apply_to_similar would match, computed by the API.
    similar_key: "MOONCAFE",
    category_id: null,
    category_source: "none",
    category_confidence: null,
    is_self_transfer: false,
    notes: null,
    created_at: "2026-11-03T10:00:00Z",
    amount_display: "₹70.00",
    ...overrides,
  };
}

/**
 * A pretend transactions API. GET /transactions answers `pages` one after
 * the other (cursor "page-2", "page-3", …); PATCH /transactions/<id>
 * applies the body to the row and answers it, or answers `patchError`.
 */
function fakeApi({ pages = [[]], patchError } = {}) {
  const rows = new Map(pages.flat().map((row) => [row.id, row]));
  function handler(path, options) {
    const method = options.method ?? "GET";
    if (path === "/accounts") return json(200, [ACCOUNT]);
    if (path === "/categories") return json(200, CATEGORIES);
    if (path.startsWith("/transactions") && method === "GET") {
      const cursor = new URLSearchParams(path.split("?")[1] ?? "").get("cursor");
      const index = cursor ? Number(cursor.replace("page-", "")) - 1 : 0;
      const next = index + 1 < pages.length ? `page-${index + 2}` : null;
      const items = (pages[index] ?? []).map((row) => rows.get(row.id));
      return json(200, { items, next_cursor: next });
    }
    const match = path.match(/^\/transactions\/([^/?]+)$/);
    if (match && method === "PATCH") {
      if (patchError) return patchError();
      const row = rows.get(match[1]);
      const body = JSON.parse(options.body);
      Object.assign(row, body.category_id !== undefined ? { category_id: body.category_id } : {});
      if (body.notes !== undefined) row.notes = body.notes;
      return json(200, row);
    }
    return undefined;
  }
  return { handler, rows };
}

const listRequests = (fetchImpl) =>
  fetchImpl.mock.calls
    .map(([url, options]) => [url.replace("/api/v1", ""), options?.method ?? "GET"])
    .filter(([path, method]) => path.startsWith("/transactions") && method === "GET")
    .map(([path]) => path);

const patches = (fetchImpl) =>
  fetchImpl.mock.calls
    .filter(([, options]) => options?.method === "PATCH")
    .map(([url, options]) => ({ path: url.replace("/api/v1", ""), body: JSON.parse(options.body) }));

async function renderView(api) {
  const rendered = renderWithSession(<TransactionsView />, { signedIn: true, handler: api.handler });
  await screen.findByRole("heading", { name: "Transactions" });
  return rendered;
}

const rowOf = (text) => screen.getByText(text, { selector: "p" }).closest("li");

describe("TransactionsView: the list", () => {
  it("shows each transaction with its date, account, signed amount and badges", async () => {
    const api = fakeApi({
      pages: [
        [
          aTransaction({ normalized_description: "MOONCAFE" }),
          aTransaction({
            normalized_description: "ASHA",
            merchant_key: "ASHA",
            similar_key: "ASHA",
            category_id: GROCERIES.id,
            category_source: "heuristic",
            category_confidence: 0.6,
          }),
          aTransaction({
            normalized_description: "OWN ACCOUNT",
            direction: "credit",
            amount_paise: 12500000,
            is_self_transfer: true,
            category_id: CATEGORIES[2].id,
            category_source: "user_manual",
          }),
        ],
      ],
    });
    await renderView(api);

    const cafe = (await screen.findByText("MOONCAFE", { selector: "p" })).closest("li");
    expect(cafe).toHaveTextContent("5 Aug 2026");
    expect(cafe).toHaveTextContent("Salary account");
    expect(cafe).toHaveTextContent("Not categorized yet");
    expect(cafe).toHaveTextContent("Money out: −₹70.00");

    expect(rowOf("ASHA")).toHaveTextContent("Guess");
    const own = rowOf("OWN ACCOUNT");
    expect(own).toHaveTextContent("Self transfer");
    expect(own).toHaveTextContent("Money in: +₹1,25,000.00");
    expect(screen.getByText("Showing 3 transactions.")).toBeInTheDocument();
  });

  it("loads the next page with the cursor the last page gave", async () => {
    const api = fakeApi({
      pages: [[aTransaction({ normalized_description: "FIRST PAGE" })], [aTransaction({ normalized_description: "SECOND PAGE" })]],
    });
    const { fetchImpl } = await renderView(api);
    await screen.findByText("FIRST PAGE");

    fireEvent.click(screen.getByRole("button", { name: "Load more" }));

    expect(await screen.findByText("SECOND PAGE")).toBeInTheDocument();
    expect(screen.getByText("FIRST PAGE")).toBeInTheDocument(); // kept, not replaced
    expect(listRequests(fetchImpl)).toContain("/transactions?cursor=page-2");
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("sends the URL's filters to the API, with 'Uncategorized' as uncategorized=true", async () => {
    navigation.search = "category=uncategorized&direction=debit&q=cafe";
    const { fetchImpl } = await renderView(fakeApi({ pages: [[aTransaction()]] }));

    await screen.findByText("MOONCAFE", { selector: "p" });
    expect(listRequests(fetchImpl)).toContain(
      "/transactions?uncategorized=true&direction=debit&q=cafe",
    );
    // The form starts from the URL's filters too.
    expect(screen.getByLabelText("Search")).toHaveValue("cafe");
  });

  it("says there is nothing yet, with the way to upload, when there are no transactions", async () => {
    await renderView(fakeApi());

    expect(await screen.findByText(/No transactions yet/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Upload a statement" })).toHaveAttribute("href", "/uploads");
  });

  it("offers to clear the filters when nothing matches them", async () => {
    navigation.search = "q=nothing";
    await renderView(fakeApi());

    fireEvent.click(await screen.findByRole("button", { name: "Clear filters" }));

    expect(navigation.push).toHaveBeenCalledWith("/transactions", { scroll: false });
  });

  it("shows WHY the API refused the filters, not its generic message", async () => {
    // The shape the real backend sends (review of PR 37): the reason is in
    // details; the top-level message is always the generic one.
    navigation.search = "from=2026-09-01&to=2026-08-01";
    const api = fakeApi();
    const handler = (path, options) =>
      path.startsWith("/transactions?")
        ? apiError(422, "VALIDATION_ERROR", "The request contains invalid data.", {
            details: [
              { field: "query", message: "Value error, 'from' must not be later than 'to'" },
            ],
          })
        : api.handler(path, options);
    renderWithSession(<TransactionsView />, { signedIn: true, handler });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /^'from' must not be later than 'to'$/,
    );
  });
});

describe("TransactionsView: the filters", () => {
  it("puts the applied filters in the URL", async () => {
    await renderView(fakeApi());
    await screen.findByLabelText("Search");

    fireEvent.change(screen.getByLabelText("Search"), { target: { value: "swiggy" } });
    fireEvent.change(screen.getByLabelText("Category"), { target: { value: "uncategorized" } });
    fireEvent.change(screen.getByLabelText("Smallest amount (₹)"), { target: { value: "100" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply filters" }));

    expect(navigation.push).toHaveBeenCalledWith(
      "/transactions?category=uncategorized&q=swiggy&min_amount=100",
      { scroll: false },
    );
  });

  it("fills in this month and last month in India's time", async () => {
    // 20:00 UTC on 31 Oct is already 1 November in India.
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-31T20:00:00Z"));
    await renderView(fakeApi());
    await screen.findByLabelText("Search");

    fireEvent.click(screen.getByRole("button", { name: "This month" }));
    expect(navigation.push).toHaveBeenLastCalledWith(
      "/transactions?from=2026-11-01&to=2026-11-30",
      { scroll: false },
    );

    fireEvent.click(screen.getByRole("button", { name: "Last month" }));
    expect(navigation.push).toHaveBeenLastCalledWith(
      "/transactions?from=2026-10-01&to=2026-10-31",
      { scroll: false },
    );
  });
});

describe("TransactionsView: changing a category", () => {
  it("asks whether to file similar transactions too, then sends ONE request with the answer", async () => {
    const api = fakeApi({ pages: [[aTransaction()]] });
    const { fetchImpl } = await renderView(api);
    const row = (await screen.findByText("MOONCAFE", { selector: "p" })).closest("li");

    fireEvent.change(within(row).getByLabelText(/Category for MOONCAFE/), {
      target: { value: GROCERIES.id },
    });

    // By name: each <optgroup> of the drop-down is a "group" too.
    const question = within(row).getByRole("group", { name: /File every transaction/ });
    expect(question).toHaveAccessibleName("File every transaction from MOONCAFE as Groceries?");
    expect(patches(fetchImpl)).toHaveLength(0); // nothing sent before the answer

    fireEvent.click(within(question).getByRole("button", { name: "Yes, all similar" }));

    // Said above the list, not in the row (which may leave a filtered list).
    expect(await screen.findByText(/Filed every transaction from MOONCAFE as Groceries/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("The others are being updated.");
    expect(patches(fetchImpl)).toEqual([
      {
        path: expect.stringMatching(/^\/transactions\/t0/),
        body: { category_id: GROCERIES.id, apply_to_similar: true },
      },
    ]);
    expect(within(row).getByLabelText(/Category for MOONCAFE/)).toHaveDisplayValue("Groceries");
  });

  it("files only this one when asked to", async () => {
    const { fetchImpl } = await renderView(fakeApi({ pages: [[aTransaction()]] }));
    const row = (await screen.findByText("MOONCAFE", { selector: "p" })).closest("li");

    fireEvent.change(within(row).getByLabelText(/Category for/), { target: { value: SALARY.id } });
    fireEvent.click(within(row).getByRole("button", { name: "Just this one" }));

    await waitFor(() => expect(patches(fetchImpl)).toHaveLength(1));
    expect(patches(fetchImpl)[0].body).toEqual({ category_id: SALARY.id });
  });

  it("sends nothing on Cancel, and puts the old choice back", async () => {
    const { fetchImpl } = await renderView(fakeApi({ pages: [[aTransaction()]] }));
    const row = (await screen.findByText("MOONCAFE", { selector: "p" })).closest("li");
    const select = within(row).getByLabelText(/Category for/);

    fireEvent.change(select, { target: { value: SALARY.id } });
    fireEvent.click(within(row).getByRole("button", { name: "Cancel" }));

    expect(select).toHaveDisplayValue("Not categorized yet");
    expect(within(row).queryByRole("group", { name: /File every/ })).not.toBeInTheDocument();
    expect(patches(fetchImpl)).toHaveLength(0);
  });

  it("doesn't ask when the API says nothing could match (similar_key null)", async () => {
    const api = fakeApi({
      pages: [[aTransaction({ normalized_description: "ATM WDL", merchant_key: null, similar_key: null })]],
    });
    const { fetchImpl } = await renderView(api);
    const row = (await screen.findByText("ATM WDL", { selector: "p" })).closest("li");

    fireEvent.change(within(row).getByLabelText(/Category for/), {
      target: { value: GROCERIES.id },
    });

    expect(within(row).queryByRole("group", { name: /File every/ })).not.toBeInTheDocument();
    await waitFor(() => expect(patches(fetchImpl)).toHaveLength(1));
    expect(patches(fetchImpl)[0].body).toEqual({ category_id: GROCERIES.id });
  });

  it("makes a row uncategorized again without asking", async () => {
    const api = fakeApi({
      pages: [[aTransaction({ category_id: GROCERIES.id, category_source: "user_manual" })]],
    });
    const { fetchImpl } = await renderView(api);
    const row = (await screen.findByText("MOONCAFE", { selector: "p" })).closest("li");

    fireEvent.change(within(row).getByLabelText(/Category for/), { target: { value: "" } });

    await waitFor(() => expect(patches(fetchImpl)).toHaveLength(1));
    expect(patches(fetchImpl)[0].body).toEqual({ category_id: null });
  });

  it("shows the API's refusal in the row", async () => {
    const api = fakeApi({
      pages: [[aTransaction()]],
      patchError: () =>
        apiError(400, "NOTHING_TO_MATCH", "This transaction has nothing similar transactions could be recognised by."),
    });
    await renderView(api);
    const row = (await screen.findByText("MOONCAFE", { selector: "p" })).closest("li");

    fireEvent.change(within(row).getByLabelText(/Category for/), { target: { value: GROCERIES.id } });
    fireEvent.click(within(row).getByRole("button", { name: "Yes, all similar" }));

    expect(await within(row).findByRole("alert")).toHaveTextContent("nothing similar");
  });
});

describe("TransactionsView: notes", () => {
  it("adds a note, and removes it when saved empty", async () => {
    const { fetchImpl } = await renderView(fakeApi({ pages: [[aTransaction()]] }));
    const row = (await screen.findByText("MOONCAFE", { selector: "p" })).closest("li");

    fireEvent.click(within(row).getByRole("button", { name: "Add note for MOONCAFE" }));
    fireEvent.change(within(row).getByLabelText("Note for MOONCAFE"), {
      target: { value: "  Coffee with Asha  " },
    });
    fireEvent.click(within(row).getByRole("button", { name: "Save" }));

    expect(await within(row).findByText("Coffee with Asha")).toBeInTheDocument();
    expect(patches(fetchImpl)[0].body).toEqual({ notes: "Coffee with Asha" });

    fireEvent.click(within(row).getByRole("button", { name: "Edit note for MOONCAFE" }));
    fireEvent.change(within(row).getByLabelText("Note for MOONCAFE"), { target: { value: "" } });
    await act(async () => {
      fireEvent.click(within(row).getByRole("button", { name: "Save" }));
    });

    await waitFor(() => expect(patches(fetchImpl)).toHaveLength(2));
    expect(patches(fetchImpl)[1].body).toEqual({ notes: null });
  });
});

describe("TransactionsView: review fixes (PR 37)", () => {
  it("waits for the categories before showing rows, so none shows a wrong category", async () => {
    const api = fakeApi({
      pages: [[aTransaction({ category_id: GROCERIES.id, category_source: "user_manual" })]],
    });
    let releaseCategories;
    const categoriesLater = new Promise((resolve) => {
      releaseCategories = () => resolve(json(200, CATEGORIES));
    });
    const handler = (path, options) =>
      path === "/categories" ? categoriesLater : api.handler(path, options);
    const { fetchImpl } = renderWithSession(<TransactionsView />, { signedIn: true, handler });

    // The transactions have arrived, the categories not yet: no rows.
    await waitFor(() => expect(listRequests(fetchImpl)).toHaveLength(1));
    expect(screen.getByText("Loading transactions…")).toBeInTheDocument();
    expect(screen.queryByText("MOONCAFE", { selector: "p" })).not.toBeInTheDocument();

    await act(async () => releaseCategories());

    const row = (await screen.findByText("MOONCAFE", { selector: "p" })).closest("li");
    expect(within(row).getByLabelText(/Category for/)).toHaveDisplayValue("Groceries");
  });

  it("keeps saying what was saved after the row has left a filtered list", async () => {
    navigation.search = "category=uncategorized";
    const target = aTransaction();
    let filed = false;
    const api = fakeApi({ pages: [[target]] });
    const handler = (path, options) => {
      if (path.startsWith("/transactions?") && filed) {
        // Filed: it no longer matches "Not categorized yet".
        return json(200, { items: [], next_cursor: null });
      }
      if ((options.method ?? "GET") === "PATCH") filed = true;
      return api.handler(path, options);
    };
    renderWithSession(<TransactionsView />, { signedIn: true, handler });
    const row = (await screen.findByText("MOONCAFE", { selector: "p" })).closest("li");

    fireEvent.change(within(row).getByLabelText(/Category for/), { target: { value: SALARY.id } });
    fireEvent.click(within(row).getByRole("button", { name: "Just this one" }));

    await waitFor(() => expect(screen.queryByText("MOONCAFE", { selector: "p" })).not.toBeInTheDocument());
    expect(screen.getByRole("status")).toHaveTextContent("Filed MOONCAFE as Salary.");
  });
});
