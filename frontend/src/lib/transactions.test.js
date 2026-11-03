import { describe, expect, it } from "vitest";

import {
  UNCATEGORIZED,
  emptyFilters,
  filtersFromSearchParams,
  hasFilters,
  isGuess,
  searchParamsFromFilters,
  signedAmount,
  similarKey,
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

  it("recognises similar transactions by UPI id first, then merchant", () => {
    expect(similarKey({ counterparty_vpa: "asha@okaxis", merchant_key: "ASHA" })).toBe(
      "asha@okaxis",
    );
    expect(similarKey({ counterparty_vpa: null, merchant_key: "MOONCAFE" })).toBe("MOONCAFE");
    // A cash withdrawal has neither: the "similar?" question isn't asked.
    expect(similarKey({ counterparty_vpa: null, merchant_key: null })).toBeNull();
  });
});
