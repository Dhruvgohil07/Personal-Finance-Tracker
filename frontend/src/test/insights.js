// Test helpers: insights API answers as the backend sends them
// (app/schemas/insights.py). Not a test file itself.
//
// The `*_display` strings are written out by hand, not made with
// formatInr: the dashboard must show what the API sent, and a fake that
// formatted its own numbers would hide a page that formats them again.

import { shiftMonth } from "@/lib/insights";
import { json } from "@/test/session";

export function aPeriod(overrides = {}) {
  return {
    month: "2026-10",
    income_paise: 0,
    spend_paise: 0,
    net_paise: 0,
    transaction_count: 0,
    uncategorized_count: 0,
    income_display: "₹0.00",
    spend_display: "₹0.00",
    net_display: "₹0.00",
    ...overrides,
  };
}

/** GET /insights/summary; `current` and `previous` override their periods. */
export function aSummary({ month = "2026-10", current, previous, ...rest } = {}) {
  const previousMonth = shiftMonth(month, -1);
  return {
    month,
    from: `${month}-01`,
    to: `${month}-31`,
    current: aPeriod({ month, ...current }),
    previous: aPeriod({ month: previousMonth, ...previous }),
    income_change_pct: null,
    spend_change_pct: null,
    ...rest,
  };
}

/** GET /insights/categories with these items (each a partial CategorySpend). */
export function aBreakdown(items = [], { from = "2026-10-01", to = "2026-10-31" } = {}) {
  return {
    from,
    to,
    total_spend_paise: items.reduce((sum, item) => sum + item.amount_paise, 0),
    items: items.map((item) => ({
      category_id: null,
      name: "Not categorized yet",
      slug: null,
      kind: null,
      icon: null,
      color: null,
      transaction_count: 1,
      share_pct: 100,
      ...item,
    })),
  };
}

/**
 * A handler (see renderWithSession) for the dashboard's requests: no
 * transactions, and an empty month for any month asked about. For tests
 * of other parts of the home page that only need the dashboard to load.
 */
export function emptyDashboardHandler(path) {
  if (path === "/transactions?limit=1") {
    return json(200, { items: [], next_cursor: null });
  }
  const summary = path.match(/^\/insights\/summary\?month=(\d{4}-\d{2})$/);
  if (summary) {
    return json(200, aSummary({ month: summary[1] }));
  }
  if (path.startsWith("/insights/categories?")) {
    return json(200, aBreakdown());
  }
  return undefined;
}
