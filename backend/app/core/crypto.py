"""Encrypt uploaded statement files at rest (SPEC §5 `upload_blobs`, §7.3).

From Phase 2 the API does not parse an upload itself: it stores the file
and an RQ worker parses it later. Between the two, the file sits in the
`upload_blobs` table, and a bank statement is about the most sensitive
thing this app will ever hold. So it is stored encrypted, with a key the
database never sees (`FILE_ENCRYPTION_KEY`, from the environment).

The algorithm is **AES-256-GCM**, through the `cryptography` package's
`AESGCM` class. Three properties of GCM matter here:

1. **Confidentiality.** Without the key the bytes are unreadable, so a
   leaked database backup or a stolen dump does not leak statements.
2. **Integrity.** GCM adds a 16-byte authentication tag. If a single bit of
   the stored ciphertext is changed, decryption fails instead of returning
   corrupted data, so the worker can never parse a tampered file.
3. **Associated data.** The upload's id is bound into the tag without being
   encrypted. A blob therefore only decrypts for the upload it was written
   for. Someone with write access to the database cannot move one user's
   statement onto another user's upload row.

The **nonce** ("number used once", 12 bytes) must never repeat for the same
key. It is random per file (`os.urandom`), which is the standard choice:
with 96 random bits a repeat is not a practical concern at this app's size.
It is not secret, so it is stored next to the ciphertext.

This module is pure (no database, no HTTP), so the tests can call it
directly. The key defaults to the one in the settings; tests pass their own.
"""

import os
import uuid
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

# AES-GCM's standard nonce length. Other lengths work but are slower and
# less studied, so the library docs recommend exactly 12 bytes.
NONCE_BYTES = 12
# AES-256: a 32-byte key. AESGCM would also accept a 16- or 24-byte key
# (AES-128/192), so the length is checked here rather than trusted.
KEY_BYTES = 32


class FileDecryptionError(Exception):
    """The stored file could not be decrypted.

    Raised for every kind of failure: the bytes were changed, the blob
    belongs to a different upload, or the key is not the one it was
    encrypted with. GCM cannot tell these apart, and the caller does not
    need to: in every case the file is unusable and the import fails.

    The message carries no detail on purpose. Nothing about the file's
    contents is known at this point, and nothing should be guessed in a log.
    """


@dataclass(frozen=True)
class EncryptedFile:
    """What is stored in `upload_blobs`: the nonce and the ciphertext.

    `ciphertext` already ends with GCM's 16-byte authentication tag
    (`AESGCM.encrypt` appends it), so it is 16 bytes longer than the file.
    """

    nonce: bytes
    ciphertext: bytes


def encrypt_file(
    content: bytes, upload_id: uuid.UUID, *, key: bytes | None = None
) -> EncryptedFile:
    """Encrypt an uploaded file for the upload `upload_id`.

    `key` is the raw 32-byte key. Leave it out to use `FILE_ENCRYPTION_KEY`.
    """
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(_key(key)).encrypt(nonce, content, _associated_data(upload_id))
    return EncryptedFile(nonce=nonce, ciphertext=ciphertext)


def decrypt_file(
    encrypted: EncryptedFile, upload_id: uuid.UUID, *, key: bytes | None = None
) -> bytes:
    """Decrypt a stored file, or raise `FileDecryptionError`.

    `upload_id` must be the id the file was encrypted for. Passing any
    other id fails, which is the point of the associated data.
    """
    try:
        return AESGCM(_key(key)).decrypt(
            encrypted.nonce, encrypted.ciphertext, _associated_data(upload_id)
        )
    except InvalidTag:
        # `from None`: the original exception adds nothing a reader of the
        # log could use, so it is not chained.
        raise FileDecryptionError("The stored file could not be decrypted.") from None


def _key(key: bytes | None) -> bytes:
    """The key to use: the one given, or the app's key from the settings."""
    if key is None:
        # Already validated as 32 bytes of base64 when the settings loaded.
        return get_settings().file_encryption_key_bytes
    if len(key) != KEY_BYTES:
        raise ValueError(f"the file encryption key must be {KEY_BYTES} bytes")
    return key


def _associated_data(upload_id: uuid.UUID) -> bytes:
    """The upload id as 16 raw bytes, bound into the authentication tag."""
    return upload_id.bytes
