// Money in the browser is only FORMATTED, never computed (Phase 3 plan, hard
// constraint 4). The API sends integer paise (₹12.50 -> 1250, CLAUDE.md rule
// 1), and totals come from the API.

// Indian digit grouping: the first comma after 3 digits, then every 2
// (1,25,000 = 1.25 lakh). "en-IN" is the locale that groups that way.
const indianGrouping = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });

/**
 * Format integer paise as Indian rupees: 12500000 -> "₹1,25,000.00".
 *
 * The same output as the backend's `format_inr` (app/utils/money.py),
 * including negatives: -125050 -> "-₹1,250.50".
 *
 * Only integer arithmetic is used. Dividing by 100 would give a float, and
 * floats can't represent most decimal fractions exactly; splitting the paise
 * into whole rupees and leftover paise avoids that entirely.
 */
export function formatInr(paise) {
  // Number.isSafeInteger: a whole number small enough to be exact in
  // JavaScript (up to about 9 * 10^15 paise, far beyond any statement).
  // Rejects floats, strings and NaN, so a wrong value fails loudly.
  if (!Number.isSafeInteger(paise)) {
    throw new TypeError("formatInr expects integer paise");
  }
  const sign = paise < 0 ? "-" : "";
  const absolute = Math.abs(paise);
  const remainder = absolute % 100;
  // Subtract the leftover first, so the division has no fraction at all and
  // its result is exact even for very large values.
  const rupees = (absolute - remainder) / 100;
  return `${sign}₹${indianGrouping.format(rupees)}.${String(remainder).padStart(2, "0")}`;
}
