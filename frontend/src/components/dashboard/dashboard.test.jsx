import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Dashboard } from "@/components/dashboard/dashboard";
import { aBreakdown, aSummary } from "@/test/insights";
import { apiError, json, renderWithSession } from "@/test/session";

// next/navigation only works inside a running Next.js app, so the router
// and the URL's query are faked (as in transactions.test.jsx).
const navigation = vi.hoisted(() => ({ push: vi.fn(), search: "" }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: navigation.push, replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(navigation.search),
  usePathname: () => "/",
}));

beforeEach(() => {
  navigation.push.mockReset();
  navigation.search = "";
  // "Today" is 4 November 2026 in India. Only Date is faked: real timers
  // keep findBy/waitFor working.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-11-04T06:00:00Z"));
});

afterEach(() => {
  vi.useRealTimers();
});

const FOOD = "c0000000-0000-4000-8000-000000000001";

// October 2026 against September, as the API would answer.
const OCTOBER = aSummary({
  month: "2026-10",
  current: {
    income_paise: 5_000_000,
    spend_paise: 182_050,
    net_paise: 4_817_950,
    transaction_count: 4,
    uncategorized_count: 2,
    income_display: "₹50,000.00",
    spend_display: "₹1,820.50",
    net_display: "₹48,179.50",
  },
  previous: {
    income_paise: 4_000_000,
    spend_paise: 100_000,
    net_paise: 3_900_000,
    transaction_count: 2,
    net_display: "₹39,000.00",
  },
  income_change_pct: 25,
  spend_change_pct: 82.1,
});

const OCTOBER_BREAKDOWN = aBreakdown([
  {
    category_id: FOOD,
    name: "Food & Dining",
    slug: "food-dining",
    kind: "expense",
    color: "#F97316",
    amount_paise: 132_050,
    amount_display: "₹1,320.50",
    transaction_count: 2,
    share_pct: 72.5,
  },
  {
    amount_paise: 50_000,
    amount_display: "₹500.00",
    transaction_count: 1,
    share_pct: 27.5,
  },
]);

/**
 * A pretend insights API: `newest` is the newest transaction's date (null
 * for none), `summaries` and `breakdowns` are keyed by month; a month not
 * listed is empty. `fail` returns an error answer for a path, or nothing.
 */
function fakeInsightsApi({ newest = "2026-10-28", summaries = {}, breakdowns = {}, fail } = {}) {
  return (path) => {
    const failure = fail?.(path);
    if (failure) {
      return failure;
    }
    if (path === "/transactions?limit=1") {
      const items = newest ? [{ id: "t1", txn_date: newest }] : [];
      return json(200, { items, next_cursor: null });
    }
    const summary = path.match(/^\/insights\/summary\?month=(\d{4}-\d{2})$/);
    if (summary) {
      return json(200, summaries[summary[1]] ?? aSummary({ month: summary[1] }));
    }
    const categories = path.match(/^\/insights\/categories\?from=(\d{4}-\d{2})-01&to=\d{4}-\d{2}-\d{2}$/);
    if (categories) {
      return json(200, breakdowns[categories[1]] ?? aBreakdown());
    }
    return undefined;
  };
}

function renderDashboard(api = {}) {
  return renderWithSession(<Dashboard />, {
    signedIn: true,
    handler: fakeInsightsApi({
      summaries: { "2026-10": OCTOBER },
      breakdowns: { "2026-10": OCTOBER_BREAKDOWN },
      ...api,
    }),
  });
}

const requested = (fetchImpl, path) => fetchImpl.mock.calls.some(([url]) => url === `/api/v1${path}`);

describe("Dashboard: which month", () => {
  it("opens on the month of the newest transaction", async () => {
    const { fetchImpl } = renderDashboard();

    expect(await screen.findByRole("heading", { name: "October 2026" })).toBeInTheDocument();
    expect(requested(fetchImpl, "/insights/summary?month=2026-10")).toBe(true);
    expect(requested(fetchImpl, "/insights/categories?from=2026-10-01&to=2026-10-31")).toBe(true);
  });

  it("opens on this month when there are no transactions", async () => {
    renderDashboard({ newest: null });

    expect(await screen.findByRole("heading", { name: "November 2026" })).toBeInTheDocument();
  });

  it("shows the URL's month without asking for the newest transaction", async () => {
    navigation.search = "month=2026-03";
    const { fetchImpl } = renderDashboard();

    expect(await screen.findByRole("heading", { name: "March 2026" })).toBeInTheDocument();
    expect(requested(fetchImpl, "/transactions?limit=1")).toBe(false);
  });

  it.each(["2026-13", "2027-01", "march"])("ignores ?month=%s", async (month) => {
    navigation.search = `month=${month}`;
    renderDashboard();

    expect(await screen.findByRole("heading", { name: "October 2026" })).toBeInTheDocument();
  });

  it("puts the month before or after in the URL", async () => {
    renderDashboard();

    fireEvent.click(await screen.findByRole("button", { name: "Show September 2026" }));
    expect(navigation.push).toHaveBeenCalledWith("/?month=2026-09", { scroll: false });

    fireEvent.click(screen.getByRole("button", { name: "Show November 2026" }));
    expect(navigation.push).toHaveBeenCalledWith("/?month=2026-11", { scroll: false });
  });

  it("can't go past this month", async () => {
    navigation.search = "month=2026-11";
    renderDashboard();

    expect(await screen.findByRole("button", { name: "Show December 2026" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Show October 2026" })).toBeEnabled();
  });
});

describe("Dashboard: the month's totals", () => {
  it("shows the API's amounts and changes against last month", async () => {
    renderDashboard();

    const moneyIn = (await screen.findByText("Money in")).parentElement;
    expect(moneyIn).toHaveTextContent("₹50,000.00");
    expect(moneyIn).toHaveTextContent("25% more than September");

    const moneyOut = screen.getByText("Money out").parentElement;
    expect(moneyOut).toHaveTextContent("₹1,820.50");
    expect(moneyOut).toHaveTextContent("82.1% more than September");

    const leftOver = screen.getByText("Left over").parentElement;
    expect(leftOver).toHaveTextContent("₹48,179.50");
    expect(leftOver).toHaveTextContent("September: ₹39,000.00");
  });

  it("says so when last month had nothing to compare with", async () => {
    renderDashboard({
      summaries: {
        "2026-10": aSummary({
          current: { transaction_count: 1, spend_paise: 7_000, spend_display: "₹70.00" },
        }),
      },
    });

    expect(await screen.findByText("Nothing came in during September")).toBeInTheDocument();
    expect(screen.getByText("Nothing went out during September")).toBeInTheDocument();
  });

  it("calls a negative month overspent", async () => {
    renderDashboard({
      summaries: {
        "2026-10": aSummary({
          current: { transaction_count: 1, net_paise: -250_000, net_display: "-₹2,500.00" },
        }),
      },
    });

    const overspent = (await screen.findByText("Overspent")).parentElement;
    expect(overspent).toHaveTextContent("-₹2,500.00");
    expect(screen.queryByText("Left over")).not.toBeInTheDocument();
  });

  it("invites an upload for a month with nothing in it", async () => {
    navigation.search = "month=2026-11";
    renderDashboard();

    expect(await screen.findByText(/Nothing in November 2026 yet\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Upload that month's statement" })).toHaveAttribute(
      "href",
      "/uploads",
    );
    expect(screen.queryByText("Money in")).not.toBeInTheDocument();
  });
});

describe("Dashboard: needs your attention", () => {
  it("links to the month's transactions without a category", async () => {
    renderDashboard();

    expect(
      await screen.findByText(/2 transactions in October need your attention/),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sort them now" })).toHaveAttribute(
      "href",
      "/transactions?from=2026-10-01&to=2026-10-31&category=uncategorized",
    );
  });

  it("speaks of one transaction in the singular", async () => {
    renderDashboard({
      summaries: { "2026-10": { ...OCTOBER, current: { ...OCTOBER.current, uncategorized_count: 1 } } },
    });

    expect(
      await screen.findByText(/1 transaction in October needs your attention: it has/),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sort it now" })).toBeInTheDocument();
  });

  it("says nothing when every transaction has a category", async () => {
    renderDashboard({
      summaries: { "2026-10": { ...OCTOBER, current: { ...OCTOBER.current, uncategorized_count: 0 } } },
    });

    expect(await screen.findByText("Money in")).toBeInTheDocument();
    expect(screen.queryByText(/need your attention/)).not.toBeInTheDocument();
  });
});

describe("Dashboard: where it went", () => {
  it("lists every category with its amount, share and count, linking to them", async () => {
    renderDashboard();

    const list = await screen.findByRole("list", { name: "Spending by category" });
    const [food, notCategorized] = within(list).getAllByRole("link");

    expect(food).toHaveTextContent("Food & Dining");
    expect(food).toHaveTextContent("₹1,320.50");
    expect(food).toHaveTextContent("72.5%");
    expect(food).toHaveTextContent("2 transactions");
    expect(food).toHaveAttribute(
      "href",
      `/transactions?from=2026-10-01&to=2026-10-31&category=${FOOD}&direction=debit`,
    );

    expect(notCategorized).toHaveTextContent("Not categorized yet");
    expect(notCategorized).toHaveTextContent("1 transaction");
    expect(notCategorized).toHaveAttribute(
      "href",
      "/transactions?from=2026-10-01&to=2026-10-31&category=uncategorized&direction=debit",
    );
  });

  it("draws a slice per category, hidden from screen readers", async () => {
    const { container } = renderDashboard();
    await screen.findByRole("list", { name: "Spending by category" });

    const chart = container.querySelector(".recharts-wrapper").closest("[aria-hidden]");
    expect(chart).toHaveAttribute("aria-hidden", "true");
    await waitFor(() => expect(container.querySelectorAll(".recharts-pie-sector")).toHaveLength(2));
  });

  it("says so when no money went out", async () => {
    renderDashboard({
      summaries: { "2026-10": { ...OCTOBER, current: { ...OCTOBER.current, spend_paise: 0 } } },
      breakdowns: { "2026-10": aBreakdown() },
    });

    expect(await screen.findByText("No money went out in October 2026.")).toBeInTheDocument();
  });

  it("keeps the totals when the breakdown fails", async () => {
    renderDashboard({
      fail: (path) =>
        path.startsWith("/insights/categories")
          ? apiError(500, "INTERNAL_ERROR", "Something went wrong.")
          : undefined,
    });

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("Money in")).toBeInTheDocument();
  });
});

describe("Dashboard: errors", () => {
  it("shows an error when the newest transaction can't be read", async () => {
    renderDashboard({
      fail: (path) =>
        path === "/transactions?limit=1"
          ? apiError(500, "INTERNAL_ERROR", "Something went wrong.")
          : undefined,
    });

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByRole("heading")).not.toBeInTheDocument();
  });
});
