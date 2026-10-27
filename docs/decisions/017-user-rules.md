# ADR 017 — User rules and re-categorization

- Status: accepted
- Date: 2026-10-27 (Phase 2, Step 7)

## Context
ADR 016 built the categorizer chain with a user-rule handler at the front, but no
rules existed yet. SPEC §8.1 says that when a user re-files a transaction with
`apply_to_similar=true`, the app creates a `category_rules` row and re-categorizes
matching past transactions (except `user_manual` ones) in a background job. SPEC §10
adds `GET/POST/PATCH/DELETE /rules`, and SPEC §11 names the job
`recategorize_by_rule(rule_id)`.

The SPEC does not say:
- what happens to past rows when a rule is **edited or deleted**;
- what "similar" means for a given row;
- what to do when the user corrects a row whose counterparty already has a rule;
- what to do when Redis is down.

## Decision

**1. Table `category_rules` (migration 0012).** It has `match_type` (`merchant` |
`vpa` | `contains`), `pattern`, `category_id`, `priority` (default 100, lower is tried
first) and `created_from_transaction_id`.
- Unique key: `(user_id, match_type, pattern)`, so there is one rule per thing
  matched.
- `category_id` is ON DELETE CASCADE: a rule for a deleted category has nothing left
  to do.
- `created_from_transaction_id` is SET NULL: deleting an upload keeps the rule the
  user taught.
- Both foreign keys are indexed, so a cascading delete does not scan the table once
  per deleted row.

**2. Patterns are stored normalized for their match type**
(`normalize_rule_pattern`, in the pure categorizer). Each one uses the function that
built the field it is compared with:
- `merchant`: `name_key` ("Moon Cafe" → `MOONCAFE`)
- `vpa`: lowercase
- `contains`: `normalize_description`

Matching is then a plain comparison, and the unique key treats "Moon Cafe" and
"MOONCAFE" as the same rule. Patterns under 3 characters are refused (422), and so are
digits-only merchant names.

**3. Every rule change re-runs the chain over ALL of the user's non-manual rows**
(decided with Dhruv). This covers create, update, delete and `apply_to_similar`, and
it is one job, `recategorize_user(user_id)`, which replaces the SPEC's
`recategorize_by_rule(rule_id)`. The invariant is easy to explain and to test: *a
non-manual row always shows what the current rules and the built-in chain say.* A
deleted rule therefore stops having an effect, which a per-rule job could not do,
because the rule is gone by the time the job runs. The cost is one pass over the
user's rows with no LLM, which is a few thousand rows for years of statements.
SQLAlchemy sends no UPDATE for a row whose values came out the same, so a re-run with
the same rules writes nothing.

**4. Ties go to the newest rule.** Rules are tried by `priority`, then
`created_at DESC`, so the latest correction wins over an older rule of the same
priority. `GET /rules` lists them in exactly that order.

**5. What a correction learns** (`learned_rule_key`, pure):
- If the row is a person (P2P shape and no dictionary merchant), the rule matches
  their **VPA**. Two people can share a name cut to 10 characters, but a VPA belongs
  to one person.
- Otherwise, if the row has a merchant key, the rule matches the **merchant key**.
  One shop pays out through many VPAs (one QR code per counter), and the key is what
  they share. Swiggy's VPA looks personal, but Swiggy is a dictionary merchant, so it
  gets a key rule.
- Otherwise the request fails with **400 `NOTHING_TO_MATCH`** (for example, a cash
  withdrawal). It is 400, not the plan's 422, for the same reason as
  `INVALID_STATEMENT`: every field is valid, and it is the transaction that cannot be
  used. 422 stays reserved for field errors.
- A `contains` rule is never learned, because picking the right part of a narration is
  exactly the guess the two options above avoid.

**6. A second correction updates the existing rule** (decided with Dhruv). It is one
`INSERT ... ON CONFLICT (user_id, match_type, pattern) DO UPDATE`, which is atomic, so
two corrections at the same moment cannot both insert. `POST /rules` with a duplicate
is still a 409. The manual category and the rule are saved in **one commit**, so a 400
leaves the row unchanged too.

**7. If Redis is down, the re-run happens inside the request** (decided with Dhruv).
Unlike an upload, there is no pending row a later retry could pick up. A 503 would
leave a rule that is saved but not applied. The fallback logs
`recategorize_enqueue_failed_running_inline`.

**8. One user's categorization runs one at a time.** Both `categorize_upload` (the
import) and `recategorize_user` first take `pg_advisory_xact_lock` on a key derived
from the user id, and only then read the rules. Whichever run goes second therefore
sees the rules the first one left, and the newest rules always have the last word. A
re-run also reads the rows `FOR UPDATE`. If the user re-files a row by hand while a
re-run is going, Postgres either makes the PATCH wait, or re-checks the WHERE clause,
sees `user_manual`, and skips the row. A manual choice is never overwritten.

**9. Rules may set `is_self_transfer`.** As with a manual choice
(`_set_category`), a row filed under a `transfer`-kind category (Self Transfer) is
flagged, and the flag is cleared when the category goes away. The flag the insights
read therefore never disagrees with the category.

**10. The job's only argument is the user id.** It is never the rule: a `vpa`
pattern is a person's UPI id, and job arguments sit in Redis unencrypted. This is
tested.

## Alternatives rejected
- **`recategorize_by_rule(rule_id)`, the SPEC's name, on create only.** Editing or
  deleting a rule would leave stale categories behind, for example a deleted "ZOMATO →
  Groceries" rule keeping those rows in Groceries forever.
- **409 when `apply_to_similar` hits an existing rule.** It forces a detour through
  `/rules` for what is, from the user's side, simply "no, this one is Rent".
- **503 when Redis is down.** The retry would hit 409 on `POST /rules`, and the
  user would have to guess what state they are in.
- **Changing a rule's pattern or match type with PATCH.** That would make it a
  different rule. Delete and re-create it, as with an account's number.
- **A per-user row lock (`SELECT ... FROM users FOR UPDATE`)** instead of an advisory
  lock. Inserting a row that references `users` (a refresh token at login) would wait
  for it. An advisory lock blocks nothing but other categorization runs.

## Consequences
- `PATCH /transactions/{id}` takes `apply_to_similar` and the job queue. Its
  `is_self_transfer` stays derived from the category (Phase 1 Step 8).
- **Step 8 must fit into point 3.** A re-run re-decides every non-manual row, so it
  would undo a self-transfer flag that Step 8's detector set. Step 8 has to run
  detection after a re-run as well (for example, enqueue `post_import_analysis` from
  the re-categorization job) or exclude detected pairs from the re-run.
- A user with very many rows makes each rule change a longer job. That is fine in the
  background, but slow in the Redis-down fallback. Revisit if a user ever has 100k+
  rows.
