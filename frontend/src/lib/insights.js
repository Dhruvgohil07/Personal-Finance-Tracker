// The dashboard's data: which month is shown, the hooks that ask the
// insights API about it, and the small rules for what the page says.
//
// A month travels as "YYYY-MM" everywhere (the URL's ?month=, the API's
// ?month=, the summary's `month` field), so the helpers here take and
// return that form. Its days come from monthRange (src/lib/dates.js), which
// works on UTC dates so no timezone can move them.
//
// Money is only shown, never computed (hard constraint 4): every amount on
// the dashboard arrives as `*_display` from the API, and every percentage
// is the API's own (`*_change_pct`, `share_pct`), only formatted here.

import { useQuery } from "@tanstack/react-query";

import { useAuth } from "@/lib/auth";
import { monthRange, todayInIndia } from "@/lib/dates";
import { UNCATEGORIZED, emptyFilters, searchParamsFromFilters } from "@/lib/transactions";

export const INSIGHTS_QUERY_KEY = ["insights"];

// ---- months ----------------------------------------------------------------

const MONTH_PATTERN = /^(\d{4})-(0[1-9]|1[0-2])$/;

/** True for "2026-08"; false for "2026-13", "2026-8" or anything else. */
export function isMonth(value) {
  return MONTH_PATTERN.test(value ?? "");
}

// The first month the summary API can answer for: it accepts years from
// 1900 (MIN_YEAR in backend app/utils/dates.py) and also reports the month
// before, so January 1900 is refused with a 422 (review of PR 38).
export const EARLIEST_MONTH = "1900-02";

/** The month a date is in: "2026-08-15" → "2026-08". */
export function monthOf(isoDate) {
  return isoDate.slice(0, 7);
}

/** This month in India's time (CLAUDE.md rule 10), whatever the browser's. */
export function currentMonth(now = new Date()) {
  return monthOf(todayInIndia(now));
}

/** `{ from, to }`: the first and last day of the month, as ISO dates. */
export function monthDays(month) {
  return monthRange(`${month}-01`);
}

/** The month `offset` months away: `shiftMonth("2026-01", -1)` → "2025-12". */
export function shiftMonth(month, offset) {
  return monthOf(monthRange(`${month}-01`, offset).from);
}

// Printed in UTC for the same reason as statement dates (src/lib/dates.js):
// the Date below is midnight UTC on the 1st, which in a timezone behind UTC
// would still be the month before.
const monthNameFormat = new Intl.DateTimeFormat("en-IN", {
  month: "long",
  year: "numeric",
  timeZone: "UTC",
});
const shortMonthNameFormat = new Intl.DateTimeFormat("en-IN", {
  month: "long",
  timeZone: "UTC",
});

function monthStart(month) {
  const [year, number] = month.split("-").map(Number);
  return new Date(Date.UTC(year, number - 1, 1));
}

/** "2026-10" → "October 2026". */
export function monthLabel(month) {
  return monthNameFormat.format(monthStart(month));
}

/** "2026-10" → "October": for the comparisons, where the year is obvious. */
export function monthName(month) {
  return shortMonthNameFormat.format(monthStart(month));
}

/**
 * True for a ?month= the dashboard can show: a real month, from
 * EARLIEST_MONTH up to `today`. "YYYY-MM" strings compare correctly as
 * text, so `>=` and `<=` are enough.
 */
export function urlMonthIsUsable(urlMonth, today) {
  return isMonth(urlMonth) && urlMonth >= EARLIEST_MONTH && urlMonth <= today;
}

/**
 * Which month the dashboard opens on (decided with Dhruv): the URL's
 * ?month= if it is valid; otherwise the month of the newest transaction,
 * since a statement usually covers a month that has already ended and the
 * current month would open nearly empty; with no transactions at all, the
 * current month. Never a month after the current one (`today`), from the
 * URL or otherwise: the picker can't go there either.
 *
 * `newestDate` is the newest transaction's `txn_date`, or null.
 */
export function chooseMonth({ urlMonth, newestDate, today }) {
  if (urlMonthIsUsable(urlMonth, today)) {
    return urlMonth;
  }
  if (newestDate) {
    const newest = monthOf(newestDate);
    return newest < today ? newest : today;
  }
  return today;
}

// ---- what the page says ------------------------------------------------------

// One decimal at most, as the API rounds them: 25 → "25", 82.1 → "82.1".
const percentFormat = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 1 });

/** 72.5 → "72.5%". A share of the month's spending, from the API. */
export function formatPercent(pct) {
  return `${percentFormat.format(pct)}%`;
}

/**
 * The comparison under a total, e.g. "25% more than September".
 *
 * `pct` is the API's `*_change_pct`: null when last month was zero, because
 * growth from nothing has no percentage (ADR 010). Then the sentence says
 * what is known instead: `emptyText`, e.g. "Nothing came in during
 * September". Whether more is good or bad depends on the card (more income
 * is good, more spending isn't), so the words stay neutral.
 */
export function changeText(pct, previousMonth, emptyText) {
  const name = monthName(previousMonth);
  if (pct === null || pct === undefined) {
    return emptyText;
  }
  if (pct === 0) {
    return `The same as ${name}`;
  }
  const direction = pct > 0 ? "more" : "less";
  return `${formatPercent(Math.abs(pct))} ${direction} than ${name}`;
}

// ---- links into the transactions page ---------------------------------------

function transactionsLink(filters) {
  return `/transactions?${searchParamsFromFilters({ ...emptyFilters(), ...filters })}`;
}

/**
 * The transactions behind one row of the category breakdown: that month,
 * that category (or "Not categorized yet"), money out only. The breakdown
 * counts debits only, so without `direction=debit` an uncategorized salary
 * would be in the list but not in the row's count.
 */
export function categoryTransactionsLink(item, month) {
  return transactionsLink({
    ...monthDays(month),
    category: item.category_id ?? UNCATEGORIZED,
    direction: "debit",
  });
}

/**
 * The month's transactions without a category, in and out: the same rows
 * the summary's `uncategorized_count` counts.
 */
export function uncategorizedTransactionsLink(month) {
  return transactionsLink({ ...monthDays(month), category: UNCATEGORIZED });
}

// The colour of the "Not categorized yet" slice, which has no category and
// so no colour from the API. A plain grey, unlike the seeded
// "Uncategorized" category's bluish #9CA3AF: they mean different things
// (src/lib/transactions.js, NOT_CATEGORIZED_LABEL). The list beside the
// chart names every slice, so colour is never the only way to tell them apart.
// One colour for both themes, so it needs 3:1 contrast (WCAG, graphics)
// against both page backgrounds: 3.4:1 on white, 5.9:1 on #0a0a0a. The
// lighter #D4D4D4 it replaced was 1.5:1 on white (review of PR 38).
export const NOT_CATEGORIZED_COLOR = "#8C8C8C";
// For a category the user made without a colour (Phase 4's custom ones).
export const FALLBACK_COLOR = "#737373";

export function sliceColor(item) {
  if (item.category_id === null) {
    return NOT_CATEGORIZED_COLOR;
  }
  return item.color ?? FALLBACK_COLOR;
}

// ---- hooks -------------------------------------------------------------------

/**
 * The newest transaction's date, or null if there are none: one row of
 * GET /transactions, which lists newest first. Only asked when the URL
 * names no month (see chooseMonth).
 */
export function useNewestTransactionDate({ enabled }) {
  const { status, client } = useAuth();
  return useQuery({
    queryKey: [...INSIGHTS_QUERY_KEY, "newest-date"],
    queryFn: async ({ signal }) => {
      const page = await client.request("/transactions?limit=1", { signal });
      return page.items[0]?.txn_date ?? null;
    },
    enabled: enabled && status === "authenticated",
  });
}

/** GET /insights/summary for `month` (and the month before it). */
export function useSummary(month) {
  const { status, client } = useAuth();
  return useQuery({
    queryKey: [...INSIGHTS_QUERY_KEY, "summary", month],
    queryFn: ({ signal }) => client.request(`/insights/summary?month=${month}`, { signal }),
    enabled: Boolean(month) && status === "authenticated",
  });
}

/** GET /insights/categories for the days of `month`. */
export function useCategoryBreakdown(month) {
  const { status, client } = useAuth();
  return useQuery({
    queryKey: [...INSIGHTS_QUERY_KEY, "categories", month],
    queryFn: ({ signal }) => {
      const { from, to } = monthDays(month);
      return client.request(`/insights/categories?from=${from}&to=${to}`, { signal });
    },
    enabled: Boolean(month) && status === "authenticated",
  });
}
