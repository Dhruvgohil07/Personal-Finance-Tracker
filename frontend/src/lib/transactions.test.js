import { describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api";
import {
  UNCATEGORIZED,
  emptyFilters,
  filtersErrorMessage,
  filtersFromSearchParams,
  hasFilters,
  isGuess,
  refreshUntilSettled,
  searchParamsFromFilters,
  signedAmount,
  similarRowsSettled,
  transactionsPath,
} from "@/lib/transactions";

const ACCOUNT = "11111111-1111-4111-8111-111111111111";
const CATEGORY = "22222222-2222-4222-8222-222222222222";

describe("filters in the URL", () => {
  it("reads every filter from the query string", () => {
    const params = new URLSearchParams({
      from: "2026-08-01",
      to: "2026-08-31",
      account_id: ACCOUNT,
      category: CATEGORY,
      direction: "debit",
      q: "swiggy",
      min_amount: "100",
      max_amount: "2500.50",
    });
    expect(filtersFromSearchParams(params)).toEqual({
      from: "2026-08-01",
      to: "2026-08-31",
      account_id: ACCOUNT,
      category: CATEGORY,
      direction: "debit",
      q: "swiggy",
      min_amount: "100",
      max_amount: "2500.50",
    });
  });

  it("drops values that can't be right, so an old or edited link still works", () => {
    const params = new URLSearchParams({
      from: "yesterday",
      account_id: "not-a-uuid",
      direction: "sideways",
      min_amount: "1,000", // rupees go as plain numbers, never a float
      max_amount: "12.345",
      q: "   ",
      unknown: "x",
    });
    expect(filtersFromSearchParams(params)).toEqual(emptyFilters());
  });

  it("writes only the filters that are set, and reads them back the same", () => {
    const filters = { ...emptyFilters(), q: "rent", category: UNCATEGORIZED };
    const params = searchParamsFromFilters(filters);
    expect(params.toString()).toBe("category=uncategorized&q=rent");
    expect(filtersFromSearchParams(params)).toEqual(filters);
  });

  it("knows whether any filter is set", () => {
    expect(hasFilters(emptyFilters())).toBe(false);
    expect(hasFilters({ ...emptyFilters(), direction: "credit" })).toBe(true);
  });
});

describe("transactionsPath", () => {
  it("is plain /transactions without filters", () => {
    expect(transactionsPath(emptyFilters())).toBe("/transactions");
  });

  it("sends a category as category_id", () => {
    const filters = { ...emptyFilters(), category: CATEGORY, from: "2026-08-01" };
    expect(transactionsPath(filters)).toBe(`/transactions?from=2026-08-01&category_id=${CATEGORY}`);
  });

  it("sends 'Uncategorized' as uncategorized=true, never as a category_id", () => {
    const filters = { ...emptyFilters(), category: UNCATEGORIZED };
    expect(transactionsPath(filters)).toBe("/transactions?uncategorized=true");
  });

  it("adds the cursor of the page to continue from", () => {
    const filters = { ...emptyFilters(), q: "rent" };
    expect(transactionsPath(filters, "abc")).toBe("/transactions?q=rent&cursor=abc");
  });
});

describe("what a row shows", () => {
  it("signs the amount from the direction; the amount itself is always positive", () => {
    expect(signedAmount({ amount_paise: 7000, direction: "debit" })).toBe("−₹70.00");
    expect(signedAmount({ amount_paise: 12500000, direction: "credit" })).toBe("+₹1,25,000.00");
  });

  it.each([
    [{ category_source: "heuristic", category_confidence: 0.6 }, true],
    [{ category_source: "heuristic", category_confidence: 0.8 }, true],
    [{ category_source: "heuristic", category_confidence: 0.95 }, false],
    [{ category_source: "merchant", category_confidence: 0.6 }, false],
    [{ category_source: "user_manual", category_confidence: null }, false],
    [{ category_source: "heuristic", category_confidence: null }, false],
  ])("marks %o as a guess: %s", (transaction, expected) => {
    expect(isGuess(transaction)).toBe(expected);
  });

});

// Review of PR 37. The details below are copied from the real backend's
// answers (asked with TestClient), not made up.
describe("filtersErrorMessage", () => {
  const validation = (details) =>
    new ApiError({
      status: 422,
      code: "VALIDATION_ERROR",
      message: "The request contains invalid data.",
      details,
    });

  it("says the reason of a rule about the filters together, not the generic message", () => {
    const error = validation([
      { field: "query", message: "Value error, 'from' must not be later than 'to'" },
    ]);
    expect(filtersErrorMessage(error)).toBe("'from' must not be later than 'to'");
  });

  it("names the filter when one filter is wrong", () => {
    const error = validation([
      { field: "query.min_amount", message: "Decimal input should have no more than 2 decimal places" },
    ]);
    expect(filtersErrorMessage(error)).toBe(
      "Smallest amount: Decimal input should have no more than 2 decimal places",
    );
  });

  it("falls back to the usual message for any other error", () => {
    const error = new ApiError({ status: 500, code: "INTERNAL_ERROR", message: "Something went wrong." });
    expect(filtersErrorMessage(error)).toBe("Something went wrong.");
  });
});

describe("after 'apply to similar'", () => {
  const page = (...items) => ({ pages: [{ items, next_cursor: null }] });
  const row = (overrides) => ({
    similar_key: "MOONCAFE",
    category_source: "none",
    category_id: null,
    ...overrides,
  });

  it("is settled once every similar row has the new category", () => {
    const done = page(row({ category_id: "food", category_source: "user_rule" }));
    expect(similarRowsSettled([done], "MOONCAFE", "food")).toBe(true);
  });

  it("is not settled while a similar row still waits for the rule", () => {
    expect(similarRowsSettled([page(row({}))], "MOONCAFE", "food")).toBe(false);
  });

  it("ignores rows filed by hand (a rule never changes them) and other keys", () => {
    const lists = [
      page(row({ category_id: "rent", category_source: "user_manual" }), row({ similar_key: "OTHER" })),
    ];
    expect(similarRowsSettled(lists, "MOONCAFE", "food")).toBe(true);
  });

  it("refetches until settled, waiting longer each time", async () => {
    const wait = vi.fn(async () => {});
    const refetch = vi.fn(async () => {});
    let calls = 0;
    const settled = () => ++calls >= 3; // the job finishes during the 3rd wait

    await expect(refreshUntilSettled({ refetch, settled, wait })).resolves.toBe(true);
    expect(refetch).toHaveBeenCalledTimes(3);
    expect(wait.mock.calls.map(([ms]) => ms)).toEqual([1000, 2000, 4000]);
  });

  it("gives up after the last delay when the job never shows", async () => {
    const wait = vi.fn(async () => {});
    const refetch = vi.fn(async () => {});

    await expect(refreshUntilSettled({ refetch, settled: () => false, wait })).resolves.toBe(false);
    expect(refetch).toHaveBeenCalledTimes(4); // 15 s in all, then the next focus refetch
  });
});
