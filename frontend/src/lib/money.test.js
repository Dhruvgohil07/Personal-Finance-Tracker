import { describe, expect, it } from "vitest";

import { formatInr } from "@/lib/money";

describe("formatInr", () => {
  // The same cases as the backend's format_inr (backend/tests/unit/test_money.py),
  // so the two always print the same amount the same way.
  it.each([
    [0, "₹0.00"],
    [5, "₹0.05"], // a leading zero in the paise
    [1250, "₹12.50"],
    [99999, "₹999.99"], // the largest amount without a comma
    [100000, "₹1,000.00"], // the first comma: after 3 digits
    [12500000, "₹1,25,000.00"], // 1.25 lakh: then every 2 digits
    [1000000000, "₹1,00,00,000.00"], // 1 crore
    [-125050, "-₹1,250.50"], // the sign goes before the ₹
    [-5, "-₹0.05"],
  ])("formats %i paise as %s", (paise, expected) => {
    expect(formatInr(paise)).toBe(expected);
  });

  it("is exact for the largest safe integer", () => {
    // 2^53 - 1 paise. The expected text is what the backend's format_inr
    // prints for the same number.
    expect(formatInr(Number.MAX_SAFE_INTEGER)).toBe("₹9,00,71,99,25,47,409.91");
  });

  it.each([[12.5], ["1250"], [Number.NaN], [null], [undefined], [2 ** 53]])(
    "rejects %s, which is not integer paise",
    (value) => {
      expect(() => formatInr(value)).toThrow(TypeError);
    },
  );
});
