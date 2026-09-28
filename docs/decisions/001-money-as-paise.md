# ADR 001 — Money as integer paise, parsed with Decimal

- Status: accepted
- Date: 2026-09-27 (Phase 0, written down in Step 5; implemented in Step 2d)

## Context
Every feature of Kharcha is built on amounts: totals, budgets, recurring
payment detection, balance reconciliation (§6.4) and deduplication
fingerprints (§6.3). All of them compare or add up money, so a rounding
error anywhere shows up everywhere.

Python's `float` is a binary fraction and cannot hold most decimal values
exactly: `0.1 + 0.2 == 0.30000000000000004`, and `int(0.29 * 100)` is `28`.
Across thousands of transactions these errors add up, and exact
comparisons (`balance_before + amount == balance_after`, "is this the same
₹499 charge as last month?") fail at random.

Bank statements give amounts as text in many shapes: `1,250.50`,
`1,250.50 Dr`, `(1,250.50)`, `₹ 1,250`, or a blank cell.

## Decision
1. **Store every amount as an integer number of paise** in a `BIGINT`
   column (₹12.50 → `1250`). For transactions (§5), `amount_paise` is
   always positive (`CHECK (amount_paise > 0)`) and a separate
   `direction` column (`debit` / `credit`) says which way the money went,
   so a sign can never be flipped by accident in a query.
2. **Parse statement text with `decimal.Decimal`, never `float()`**, then
   convert to `int` paise. One function does this:
   `app/utils/money.py::parse_amount_to_paise`. It handles commas,
   currency prefixes, `Cr`/`Dr` suffixes, bracketed negatives and blank
   cells, and **rejects** more than 2 decimal places instead of rounding.
   It returns a *signed* value (negative = debit), because that is what
   the statement text says; the parser layer turns it into
   `amount_paise` + `direction` before saving.
3. **Format only at the edge** (API output / UI) with `format_inr`, which
   uses Indian lakh grouping: `12500000` → `₹1,25,000.00`.
4. Parse errors never include the input text (it is an amount, and error
   messages can reach logs; see rule 3 in CLAUDE.md).

## Alternatives rejected
- *`float`*: inexact, as shown above. Ruled out by the project rules.
- *Postgres `NUMERIC(12,2)` + Python `Decimal` everywhere*: exact and a
  common choice, but every layer (SQLAlchemy, Pydantic, JSON, the
  JavaScript frontend) must then carry `Decimal` correctly. JSON has no
  decimal type, and JavaScript numbers are floats, so the value would be
  sent as a string or silently become a float. Integers need no special
  handling anywhere and are exact in JavaScript up to 2^53 paise
  (≈ ₹90 lakh crore).
- *`INTEGER` (32-bit) paise*: max ≈ ₹2.1 crore. A home loan or an annual
  total could overflow; `BIGINT` removes the question for 8 bytes/row.
- *Storing rupees and paise in two columns*: more code for every sum and
  comparison, no benefit.
- *Rounding amounts with 3+ decimals*: would silently change money. A
  statement amount with 3 decimals means the parser misread a column, so
  we fail loudly instead.

## Consequences
- Sums, comparisons and fingerprints are exact integer operations, also
  in SQL (`SUM(amount_paise) ... WHERE direction = 'debit'`).
- Code must never divide paise with `/` (gives a float); use `//` or
  `Decimal`, and convert to rupees only for display.
- API consumers see paise, not rupees; the frontend must format them
  (a JS formatter mirroring `format_inr` is needed in Phase 3).
- Column names end in `_paise` (e.g. `amount_paise`) so the unit is never
  ambiguous.
