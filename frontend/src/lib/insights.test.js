import { describe, expect, it } from "vitest";

import {
  FALLBACK_COLOR,
  NOT_CATEGORIZED_COLOR,
  categoryTransactionsLink,
  changeText,
  chooseMonth,
  currentMonth,
  formatPercent,
  isMonth,
  monthDays,
  monthLabel,
  monthName,
  shiftMonth,
  sliceColor,
  transactionCount,
  uncategorizedTransactionsLink,
} from "@/lib/insights";

describe("isMonth", () => {
  it.each(["2026-01", "2026-12", "1999-09"])("accepts %s", (value) => {
    expect(isMonth(value)).toBe(true);
  });

  it.each(["2026-13", "2026-00", "2026-8", "2026-08-01", "august", "", null, undefined])(
    "rejects %s",
    (value) => {
      expect(isMonth(value)).toBe(false);
    },
  );
});

describe("currentMonth", () => {
  it("is the month in India, not in UTC", () => {
    // 31 Oct 20:00 UTC is already 1 Nov 01:30 in India.
    expect(currentMonth(new Date("2026-10-31T20:00:00Z"))).toBe("2026-11");
    expect(currentMonth(new Date("2026-10-31T10:00:00Z"))).toBe("2026-10");
  });
});

describe("monthDays and shiftMonth", () => {
  it("gives a month's first and last day, leap years included", () => {
    expect(monthDays("2026-08")).toEqual({ from: "2026-08-01", to: "2026-08-31" });
    expect(monthDays("2028-02")).toEqual({ from: "2028-02-01", to: "2028-02-29" });
  });

  it.each([
    ["2026-08", -1, "2026-07"],
    ["2026-01", -1, "2025-12"],
    ["2026-12", 1, "2027-01"],
    ["2026-03", 0, "2026-03"],
  ])("shifts %s by %i to %s", (month, offset, expected) => {
    expect(shiftMonth(month, offset)).toBe(expected);
  });
});

describe("month names", () => {
  it("names the month in full, with and without the year", () => {
    expect(monthLabel("2026-10")).toBe("October 2026");
    expect(monthName("2026-01")).toBe("January");
  });
});

describe("chooseMonth", () => {
  const today = "2026-11";

  it("takes a valid month from the URL first", () => {
    expect(chooseMonth({ urlMonth: "2026-03", newestDate: "2026-10-31", today })).toBe("2026-03");
  });

  it("otherwise opens on the newest transaction's month", () => {
    expect(chooseMonth({ urlMonth: "2026-13", newestDate: "2026-10-31", today })).toBe("2026-10");
    expect(chooseMonth({ urlMonth: null, newestDate: "2026-11-02", today })).toBe("2026-11");
  });

  it("never opens on a month after this one", () => {
    expect(chooseMonth({ urlMonth: null, newestDate: "2027-01-05", today })).toBe(today);
    expect(chooseMonth({ urlMonth: "2026-12", newestDate: "2026-10-31", today })).toBe("2026-10");
  });

  it("opens on this month when there are no transactions", () => {
    expect(chooseMonth({ urlMonth: null, newestDate: null, today })).toBe(today);
  });
});

describe("formatPercent and changeText", () => {
  it("shows at most one decimal, as the API rounds", () => {
    expect(formatPercent(25)).toBe("25%");
    expect(formatPercent(82.1)).toBe("82.1%");
    expect(formatPercent(0.5)).toBe("0.5%");
  });

  it("says more or less than last month", () => {
    expect(changeText(25, "2026-09", "—")).toBe("25% more than September");
    expect(changeText(-12.5, "2026-09", "—")).toBe("12.5% less than September");
    expect(changeText(0, "2026-09", "—")).toBe("The same as September");
  });

  it("uses the given words when last month had nothing to compare with", () => {
    expect(changeText(null, "2026-09", "Nothing came in during September")).toBe(
      "Nothing came in during September",
    );
  });
});

describe("transactionCount", () => {
  it("is singular for one", () => {
    expect(transactionCount(1)).toBe("1 transaction");
    expect(transactionCount(0)).toBe("0 transactions");
    expect(transactionCount(3)).toBe("3 transactions");
  });
});

describe("links into the transactions page", () => {
  const groceries = { category_id: "c0000000-0000-4000-8000-000000000001" };
  const notCategorized = { category_id: null };

  it("links a category row to that month's debits in that category", () => {
    const url = new URL(categoryTransactionsLink(groceries, "2026-08"), "http://x");
    expect(url.pathname).toBe("/transactions");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      from: "2026-08-01",
      to: "2026-08-31",
      category: groceries.category_id,
      direction: "debit",
    });
  });

  it("links the uncategorized bucket with the transactions page's own filter value", () => {
    const url = new URL(categoryTransactionsLink(notCategorized, "2026-08"), "http://x");
    expect(url.searchParams.get("category")).toBe("uncategorized");
    expect(url.searchParams.get("direction")).toBe("debit");
  });

  it("links 'needs your attention' to money in and out", () => {
    const url = new URL(uncategorizedTransactionsLink("2026-08"), "http://x");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      from: "2026-08-01",
      to: "2026-08-31",
      category: "uncategorized",
    });
  });
});

describe("sliceColor", () => {
  it("uses the category's colour, grey for no category, a fallback otherwise", () => {
    expect(sliceColor({ category_id: "c1", color: "#F97316" })).toBe("#F97316");
    expect(sliceColor({ category_id: null, color: null })).toBe(NOT_CATEGORIZED_COLOR);
    expect(sliceColor({ category_id: "c1", color: null })).toBe(FALLBACK_COLOR);
  });
});
