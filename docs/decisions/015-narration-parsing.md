# ADR 015 — Narration parsing: channel, VPA and merchant key

- Status: accepted
- Date: 2026-10-26 (Phase 2, Step 5)

## Context
Every transaction carries the bank's narration, and three facts the rest of Phase 2
needs are hidden in it: the **channel** (UPI, IMPS, ATM, …), the counterparty's
**UPI id (VPA)**, and a short **merchant key** to group a counterparty's rows. The
categorizer (Step 6), user rules (Step 7) and recurring detection (Phase 4) all
read them. Phase 1 stored `channel = other` and no VPA. It computed a first-word
merchant key and threw it away, and that key was written before any real narration
had been seen (SPEC §6.2: "do not guess bank formats").

One hard constraint (Phase 2 plan, constraint 1): the dedupe fingerprint includes
`normalize_description(narration)`. Changing that string by one character would
give every stored row a new fingerprint, and re-uploading an overlapping statement
would then duplicate all of it (CLAUDE.md rule 5).

The 175 real narrations of the ICICI statement were profiled with only their
SHAPES printed (names and VPAs masked):

| First field | Rows | Meaning |
|---|---|---|
| `UPI` | 171 | UPI (146 debit, 25 credit) |
| `MMT/IMPS/…` | 2 | IMPS credits |
| `NFS/CASH WDL/…` | 1 | cash from another bank's ATM |
| `UPL/…/UPI L…` | 1 | UPI Lite wallet top-up |

170 of the 171 UPI rows share one **fixed-width** layout:

```
UPI/<name ≤10>/<vpa ≤14>/<remark ≤10>/<payer bank ≤10>/<12-digit RRN>/<35-char txn id>[/]
```

Each field is **cut** to its width. 87 VPAs lost their `@handle` entirely
(`gpay-123456789`), others keep a cut handle (`…@`, `…@ok`). One shop is printed
both as `SHINE STAR` and `SHINESTAR`. Some rows end with a `/` and others do not.
75 UPI rows are merchant QR codes, recognisable by their VPA pattern (`Q#########@ybl`,
`gpay-…`, `paytmqr…`, `vyapar.…`, `paytm-…`).

## Decision

**1. A golden test first** (`tests/unit/test_golden_fingerprints.py`). It holds
18 narrations shaped like Indian bank narrations, with their normalized strings and
fingerprints as **literal values** copied from the unchanged code, so the test
cannot agree with a change. A deliberately changed `\d{6,}` → `\d{7,}` makes it
fail.

**2. A new pure module `app/parsers/narration.py`, kept OUT of the fingerprint.**
`normalize_description` stays byte-for-byte as it is. `NormalizedRow` gains
`channel` and `counterparty_vpa` (next to `merchant_key`), and only
`normalized_description` is hashed. `Channel` moves to `app/parsers/base.py`, for the
same reason as `Direction`: the parser decides it and the model imports it. It is
still importable from `app.models`.

**3. ICICI's layouts are read by POSITION, not by searching.** The 12-digit RRN in
field six confirms the UPI layout; then field two is the name and field three the
VPA. `MMT/IMPS/<RRN>/<remark>/<name>/<bank>` is read the same way. SPEC §6.5's VPA
regex needs the `@handle`, so it would miss the 87 cut VPAs. Narrations outside
these layouts use the generic rules (point 6).

**4. `counterparty_vpa` = the VPA field AS PRINTED, lowercased** (decided with
Dhruv). Kept even when it is cut short: one counterparty is always cut the same
way, so the value still identifies it for P2P detection and VPA rules. It is not
guaranteed to be a complete, payable VPA.

**5. `merchant_key` from the name field = the WHOLE field, joined** (decided with
Dhruv): uppercased, company filler (`PVT`, `LTD`, `INDIA`, …) and digit-only words
dropped, then the rest joined: `MOON CAFE` and `MOONCAFE` → `MOONCAFE`. This is
the only rule that turns the real file's two spellings of one shop into one key.
A name that is only a phone number gives no key.

**6. Generic rules for everything else.**
- **Channel** = the narration's FIRST word. From the real file: `UPI`, `UPL` → upi,
  `MMT` → imps, `NFS` → atm. From SPEC §6.5's list, **not yet seen in real data**
  (decided with Dhruv): `IMPS`, `NEFT`, `RTGS`, `ACH`/`NACH` → ach_nach,
  `ATM`/`ATW` → atm, `POS` → card. Interest and charges are NOT guessed: their
  wording is not a first word and differs by bank. They stay `other` until a real
  statement shows ICICI's.
- **VPA**: SPEC's pattern, on UPI rows only (an email in a NEFT remark is not a VPA).
- **Merchant key**: Phase 1's first name-like word, now skipping anything around an
  `@`, so a bank handle (`YBL`) is never the key.
- **ATM rows** get no VPA and no merchant key: cash has no counterparty.

**7. `transactions.merchant_key` is stored** (migration 0011: nullable TEXT +
`ix_transactions_user_merchant_key (user_id, merchant_key)`). `merchant_id` only
links rows whose key is in the global dictionary, and most real counterparties
(local shops, people) never will be. Step 7's rules and Phase 4's recurring
detection group by the key itself. Existing rows are not back-filled (dev data:
delete the upload and import it again).

**8. The global merchant seed is NOT extended** (decided with Dhruv). The
businesses in the real file are local shops and campus vendors. In a committed,
global dictionary they would reveal the user's location and help no other user.
Step 7's user rules learn them per user.

## Alternatives rejected
- **Improve `normalize_description` itself** (strip the 35-char txn id, drop the
  cut VPA): every stored fingerprint would change, so it would need a migration
  recomputing them, for no dedupe benefit.
- **SPEC's VPA regex everywhere**: it misses about half of ICICI's VPAs.
- **`counterparty_vpa` only when an `@` is present**: 87 rows would lose the one
  field that tells a person from a shop.
- **First word of the name field as the key**: splits `SHINE STAR`/`SHINESTAR` and
  merges different people who share a first name (3 such merges on the real file).
- **A bank-code argument to `read_narration`**: the parser layer does not pass the
  bank through `NormalizedRow`, and the RRN check already confirms the layout. A
  different bank printing the identical 7-field shape would be read the same way,
  which is acceptable.

## Consequences
- Real statement: 172 UPI / 2 IMPS / 1 ATM; 170 of 172 UPI rows have a VPA (90
  complete); 174 of 175 rows have a merchant key (the ATM row has none);
  normalized descriptions and fingerprints are **identical to before** on all 175.
- For a person-to-person payment the merchant key is the person's (cut) name. It
  stays in that user's rows. Step 6 must keep P2P out of the global cache and away
  from the LLM (SPEC §8.3), and must not key `merchant_category_cache` by it.
- A VPA rule written from an `.xls` row (cut VPA) will not match a PDF row of the
  same counterparty if the PDF prints the full VPA. This affects rules, not dedupe.
  Revisit in Step 9 when the PDF is in hand.
- Still open, no real sample: generic NACH narrations key on `EMI` (not the
  lender), and NEFT salary credits key on the IFSC bank code (`HDFC`), not the
  employer. Both are pinned in `test_narration.py` as known gaps.
- The 35-character UPI txn id stays inside `normalized_description`. Harmless for
  dedupe, but Step 9 must check that the PDF prints the same text, or the `.xls`
  and PDF of one period would not dedupe.
