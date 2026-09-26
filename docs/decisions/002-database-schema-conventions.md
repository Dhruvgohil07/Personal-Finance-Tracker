# ADR 002 — Database schema conventions

- Status: accepted
- Date: 2026-09-26 (Phase 0, Step 3)

## Context
The first migration creates `categories` and `merchants`. Choices made here
become the pattern every later table follows, so they are recorded once.

## Decisions

1. **Enums are `VARCHAR` + `CHECK`, not native Postgres `ENUM` types.**
   In Python they are `StrEnum`s (e.g. `CategoryKind`); SQLAlchemy's
   `Enum(..., native_enum=False, create_constraint=True)` stores the value as
   text and adds a `CHECK (kind IN (...))` constraint.
2. **System vs user categories share one table**: `user_id IS NULL` means a
   system category. Uniqueness is `UNIQUE NULLS NOT DISTINCT (user_id, slug)`
   (Postgres 15+), so there is exactly one system row per slug.
3. **`categories.user_id` exists from the first migration, without a foreign
   key.** The `users` table arrives in Phase 1; that migration adds
   `FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE`.
4. **UUID primary keys**, generated in Python (`uuid4`) with
   `gen_random_uuid()` as a database default for raw-SQL inserts.
5. **Constraint naming convention** on `Base.metadata`
   (`pk_`, `fk_`, `uq_`, `ck_`, `ix_` prefixes), so Alembic can find and alter
   constraints by a predictable name.
6. **Seeds are upserts** (`INSERT ... ON CONFLICT DO UPDATE`) keyed on the
   natural key (slug / normalized_key), so the seed is idempotent.

## Alternatives rejected
- *Native Postgres ENUM*: adding or removing a value needs `ALTER TYPE`, which
  Alembic autogenerate doesn't detect, and removing a value is not supported
  at all without recreating the type.
- *Two partial unique indexes* instead of `NULLS NOT DISTINCT`: works on any
  Postgres version, but it's two objects to maintain and explain; we require
  Postgres 16 anyway.
- *Adding `user_id` only in Phase 1*: simpler now, but changes the table's
  shape and uniqueness rule later. Adding just the FK later is a smaller change.
- *Check-then-insert seeding*: two round trips and a race condition between
  the check and the insert; the upsert is one atomic statement.

## Consequences
- Until Phase 1, the database does not stop a `categories.user_id` that
  points at nothing. No code writes user categories before then.
- `merchants.default_category_slug` is a slug, not a foreign key (slugs are
  unique only per user). A unit test checks every seeded slug exists.
- The seed never deletes rows: removing an entry from `seed_data.py` leaves
  the old row in the database; that needs a data migration.
- Raw SQL `UPDATE`s must set `updated_at` themselves (`onupdate` is ORM-only).
