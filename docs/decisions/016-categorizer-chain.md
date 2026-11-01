# ADR 016 — Categorizer chain

- Status: accepted
- Date: 2026-10-27 (Phase 2, Step 6)

## Context
Until now every imported row had `category_source = none`: the user had to file
each transaction by hand. SPEC §8 describes an automatic categorizer as a **chain of
handlers**: user manual, user rules, merchant dictionary, keyword rules, a
person-to-person heuristic, the LLM (Phase 4), and a default. Phase 2's
acceptance target is **more than half of the real statement categorized without an
LLM**.

Step 5 (ADR 015) gave every row the facts the chain needs: `channel`,
`counterparty_vpa` and `merchant_key`. Measured on the real ICICI statement before
building anything (175 rows):
- 16 rows match the seeded merchant dictionary
- 1 is a cash withdrawal
- 72 are UPI rows with a personal-looking VPA (49 debits, 23 credits)
- 77 are merchant QR codes of local shops that are not in the dictionary
- 9 are other rows

Without the 23 P2P credits the target is out of reach (38%).

## Decision

**1. A pure package `app/categorizer/`, like `app/parsers/`.** `base.py` holds:
- `TxnFacts`: direction, channel, normalized description, merchant key, VPA and
  current source. There is deliberately no amount, date or raw narration.
- `Decision`: category, source, confidence.
- `Handler`, a `Protocol`.
- `categorize(facts, chain)`.

`handlers.py` holds the handlers and `build_chain`. The merchant dictionary and the
user's rules are passed in as plain data. A test enforces that the package imports
no FastAPI, SQLAlchemy or Redis. `CategorySource` moves here from the model, the same
rule as `Direction` and `Channel`: the layer that decides it owns it, and the model
imports it.

**2. The chain, in order. The first handler that decides wins.**

| # | Step | Decision | Source, confidence |
|---|---|---|---|
| 1 | User manual | a guard in `categorize`, not a handler: a `user_manual` row is never touched | — |
| 2 | `UserRuleHandler` | the user's rules, lowest `priority` number first: `merchant` (key equals), `vpa` (equals), `contains` (in the narration) | `user_rule`, 1.00 |
| 3 | `MerchantHandler` | `dictionary_key` (ADR 015 §5a); a **debit** gets the merchant's category, a **credit** is **Refunds** — unless it carries an income keyword, which is passed on to the keyword handler | `merchant`, 0.90 |
| 4 | `KeywordHandler` | see point 4 | `keyword`, 0.90 |
| 5 | `P2PHandler` | see point 5 | `heuristic`, 0.70 |
| 7 | default | nothing decided: the row stays NULL ("Not categorized yet") | `none` |

Step 6 (the LLM) slots in before the default in Phase 4. User rules have no table
until Step 7, so the service passes an empty list; the handler is built and tested
now.

**3. A decision names its category by slug OR by id, exactly one.** The built-in
handlers name SYSTEM categories by slug (`food-dining`). A user rule points at a
category id, because a user's own category's slug is only unique per user.

**4. Keyword rules** (decided with Dhruv: SPEC's list, marked unseen):
- **From the real statement:**
  - the ATM channel on a debit → Cash Withdrawal
  - a credit paid by NPCI (merchant key `NPCI…`) whose narration contains `CASHBA`
    → Refunds (BHIM cashback; ICICI cuts the remark to `BHIMCASHBA`)
- **From SPEC §8, not yet seen in a real statement:**
  - a credit with `INTEREST` or `INT.PD` → Interest
  - a credit with `SALARY` → Salary
  - a NACH/ACH debit with `EMI` → EMI & Loans
  - a debit with `CHARGES`/`CHARGE`/`CHRG`/`CHGS` → Fees & Charges

Each rule checks the DIRECTION and matches WHOLE words: interest paid on a loan is
not income, and "SALARYMAN" is not salary.

The bank-wording rules (interest, salary, charges) **never read a UPI or IMPS
narration** (added in the review). Both layouts contain a remark TYPED BY THE PAYER,
so the word may be theirs, not the bank's. The review reproduced the bug: a person
paid with the remark "charges" became Fees & Charges. A remark is free text and
says what the money was for, not what kind of transaction it was. The cashback rule
names its payer for the same reason.

**5. Person-to-person (SPEC §8.3).** A UPI row with a VPA is P2P when:
- the VPA is not a merchant QR code. The five shapes seen in the real file:
  `q<digits>@`, `gpay-<digit>`, `paytmqr`, `paytm-<digit>`, `vyapar.<digit>`.
- no business word appears in the counterparty's NAME (`STORE`, `CAFE`, `MEDICAL`,
  `COLLEGE`, `PVT`, …), a first guess. When the layout is known, only the name
  field is read (`counterparty_name` in narration.py, added in the review), so a
  remark such as "hotel" does not stop a real P2P payment. Otherwise the whole
  narration is read.
- no merchant matched, which the chain's order guarantees.

**5a. Salary from a merchant employer** (added in the review). Swiggy, Zomato,
Amazon, Flipkart and Airtel are large employers as well as seeded merchants. A
credit from one of them that carries an income keyword is passed on by the
merchant handler, so it becomes Salary or Interest, not a Refund. The income check
(`_income_slug`) is one function shared by both handlers.

A **debit** is Transfers to People. A **credit** is **Other Income** (decided with
Dhruv): the summary already counts it as income, so the label agrees with the totals.

**6. Where it runs.** `services/categorization.categorize_upload(db, upload)`:
- runs inside `process_upload`, in the SAME database transaction as the insert, so
  rows appear already categorized and a failure rolls both back;
- touches only that upload's rows still at `none`, so rows skipped as duplicates
  belong to the upload that inserted them;
- looks up `dictionary_key` ONCE per row and passes it in `TxnFacts`; it sets
  `merchant_id` from that key for every row, whoever decides the category;
- passes the counterparty's name field in `TxnFacts` too. The chain never receives
  the raw narration;
- logs `rows_categorized` in `statement_imported`.

If a slug is missing from the database (the seed and the code disagree), the row is
left uncategorized and the import does not fail. A category ID from a user rule is
written only if it is a system category or the user's own (added in the review).
ADR 010's unscoped `LEFT JOIN categories` relies on exactly that, so it is enforced
where the ID is written, not only where rules are created. Both problems are logged
ONCE per upload, with a row count, not once per row.

**7. The insights bucket for NULL categories is renamed "Not categorized yet"**, so it
no longer shares its name with the seeded "Uncategorized" category (the user's
explicit "I looked, nothing fits").

## Alternatives rejected
- **One function with an if/elif per rule**: shorter today, but the order is the
  policy, and a chain of small handlers keeps each step testable. Phase 4's LLM and
  Step 7's rules become one more item in the list.
- **Categorizing in a separate job after the import**: rows would first appear
  uncategorized and then change, and it is one more job to explain. Phase 4's LLM
  step, which needs batching and retries, will be that separate job.
- **P2P credits left uncategorized**: 38% instead of 51%, below the Phase 2 target.
- **P2P credits as Transfers to People**: an expense-kind category named "to
  people", holding money received.
- **Merchant credits in the merchant's category**: a Zomato refund would count
  under Food & Dining.
- **Writing the seeded "Uncategorized" category when nothing matches**: it would
  hide "needs your attention" inside a real category (Phase 2 plan).

## Consequences
- **Real statement: 89 of 175 rows (50.9%) categorized**, with no LLM:
  - 49 Transfers to People and 19 Other Income (P2P)
  - 13 Food & Dining and 1 Groceries (dictionary)
  - 6 Refunds (4 BHIM cashback, 2 merchant refunds)
  - 1 Cash Withdrawal

  This meets the "more than half" target, but narrowly.
- The 77 QR payments at local shops (44%) stay uncategorized. Only Step 7's user
  rules (learned from one correction each) and Phase 4's LLM can place them.
- The P2P heuristic is a guess. An institution paid through a personal-looking VPA
  is filed as Transfers to People; one such payee accounts for 11 rows of the real
  statement. One correction with `apply_to_similar` (Step 7) fixes all of its rows.
- P2P rows (`heuristic`, Transfers to People / Other Income) carry real people's
  names in `merchant_key`. Phase 4 must never send them to the LLM or the global
  `merchant_category_cache` (SPEC §8.3).
- The categorizer never sets `is_self_transfer` and never picks a transfer-kind
  category; self transfers are Step 8.
- No migration: `category_source`'s values and `merchant_id` already existed.

## Amendment — Phase 2 acceptance (2026-11-01)

A sixth merchant shape, found by the acceptance run on the real October PDF:
Google Pay for Business's **handle**, `…@okbizaxis`. ICICI prints the id cut at
14 characters, so it appears as `…@okbizax`, and it is matched as the prefix
`[^@]*@okbiz`. 45 debits to 3 payees used it and were filed as "Transfers to
People". With the pattern they are `none` until corrected (decided with Dhruv).
The pattern is the handle only: `@okaxis`, Google Pay's personal handle, stays
a person, and so does an id that merely starts with "okbiz". Rule learning asks
the same question (`looks_like_person`), so correcting such a payment now teaches
a merchant rule instead of a VPA rule. See
[docs/phase-2-acceptance.md](../phase-2-acceptance.md).
