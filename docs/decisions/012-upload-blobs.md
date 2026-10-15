# ADR 012 — Uploaded files wait for the worker in Postgres, encrypted with AES-256-GCM

- Status: accepted
- Date: 2026-10-15 (Phase 2, Step 1)

## Context
From Phase 2, `POST /uploads` stores the file and an RQ worker parses it later
(SPEC §6.1). The API and the worker are separate processes, and on the planned
free hosting (SPEC §15) separate services, with **no shared disk**. The file
needs a place both can reach for the minutes between upload and processing.

A bank statement is the most sensitive thing this app holds: every transaction,
balance, counterparty and often the holder's name and address. SPEC §7.3
requires uploaded files to be encrypted at rest and deleted after processing.

## Decision

**Storage: a Postgres table, `upload_blobs`** (migration 0006,
`app/models/upload_blob.py`):

| Column | Meaning |
|---|---|
| `upload_id` | primary key **and** foreign key to `statement_uploads`, `ON DELETE CASCADE` |
| `encrypted_content` | `bytea`: the ciphertext, ending with GCM's 16-byte tag |
| `nonce` | `bytea`, `CHECK (octet_length(nonce) = 12)` |
| `created_at` | when it was stored, read by the 24-hour cleanup job (Step 3) |

An upload has at most one blob. The blob is deleted when processing finishes or
fails. The cleanup job deletes any blob older than 24 hours.

**Encryption: AES-256-GCM in the application** (`app/core/crypto.py`, the
`cryptography` package's `AESGCM`):

- **Key:** `FILE_ENCRYPTION_KEY`, 32 random bytes, base64 in the environment,
  validated at startup (`app/core/config.py`). It never enters the database.
  `crypto.py` refuses any key that is not exactly 32 bytes, because the library
  would silently accept a 16- or 24-byte key and quietly downgrade to
  AES-128/192.
- **Nonce:** 12 random bytes per file (`os.urandom`), stored next to the
  ciphertext. It is not secret, but it must never repeat under one key.
- **Associated data:** the upload's id. It is not encrypted, but it is covered
  by the authentication tag. A blob therefore decrypts only for the upload it
  was written for.
- **Any failure is one error.** A changed byte, a wrong upload id or a wrong key
  all raise `FileDecryptionError`, with a message that carries no detail. GCM
  cannot tell these apart, and the worker does not need to: the file is
  unusable in every case.

What GCM gives over plain encryption, and why each property is needed here:

1. **Confidentiality.** A database backup or leaked dump contains no readable
   statements.
2. **Integrity.** A tampered or corrupted blob fails to decrypt instead of
   reaching the parser as subtly wrong data that could import wrong amounts.
3. **Binding to the upload** (the associated data). Someone able to write to
   the database cannot move one user's statement onto another user's upload
   row and have it imported into the wrong account.

## Alternatives rejected

**Object storage (S3 / Cloudflare R2).** This is the usual home for files, and
it scales better. It is rejected for now: it adds a third service, its
credentials and its failure modes to a free-tier deployment, to hold files of
at most 10 MB for minutes. Revisit if uploads get large or frequent. The worker
only needs "bytes for upload X", so the change would stay inside the service
that loads the blob.

**Redis.** It is already shared by the API and the worker. But Redis is the job
queue, held in memory, and possibly evicted or persisted to disk with its own
rules. Large binary values do not belong there. Keeping files out of Redis
also keeps the rule simple: Redis holds job ids, never file contents or
passwords (CLAUDE.md §7.2).

**The local disk.** It does not work: the API and the worker share no disk in
the target deployment.

**Encrypting inside Postgres with `pgcrypto`.** The key would have to be sent
to the database in every query. That puts it in reach of the database's own
logs and statistics (`pg_stat_statements`), and it means a database
compromise exposes the key along with the data. Encrypting in the
application keeps the key and the data in different places.

**Fernet** (`cryptography`'s high-level recipe). It is safe and simpler to
call, but it is AES-128-CBC with HMAC-SHA256, not the AES-256-GCM that SPEC
§7.3 specifies, and it has no associated data, so binding a blob to its upload
would need extra code.

## Consequences
- The database grows temporarily, by about one file per upload in flight
  (≤ 10 MB each). Ciphertext does not compress, so Postgres stores it at full
  size until the row is deleted.
- **Losing the key loses only in-flight uploads.** Blobs live for at most 24
  hours. Imported transactions are ordinary rows and do not depend on the key.
- **Key rotation has no versioning.** To change the key, stop accepting
  uploads, let the worker drain (or let the leftovers expire and fail), then
  deploy the new key. A stored blob cannot be decrypted with the new key and
  would simply fail with "could not be decrypted". This is acceptable for data
  that lives at most a day. Revisit if blobs ever become long-lived.
- **Nonce reuse is not a practical risk at this scale.** The usual guidance for
  random 96-bit nonces is to stay well below 2^32 encryptions per key. This
  app does a handful per user per month.
- The encryption is tested by its failure cases, not only the round trip:
  a flipped bit (start, middle, tag), a changed nonce, another upload's id, the
  wrong key, and a short key (`tests/unit/test_crypto.py`). Real Postgres is
  covered in `tests/integration/test_upload_storage.py`.
