"""File encryption for upload blobs (app/core/crypto.py, ADR 012).

The round trip is the easy part. The tests that matter are the ones where
decryption must FAIL: a changed byte, the wrong upload, the wrong key. Each
of those is a situation where returning bytes would be worse than raising,
because the worker would then parse a file that is not the one the user
uploaded.
"""

import uuid

import pytest

from app.core.config import get_settings
from app.core.crypto import (
    KEY_BYTES,
    NONCE_BYTES,
    EncryptedFile,
    FileDecryptionError,
    decrypt_file,
    encrypt_file,
)

UPLOAD_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_UPLOAD_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
KEY = bytes(range(KEY_BYTES))  # a fixed 32-byte test key
OTHER_KEY = bytes(reversed(KEY))

# Stands in for a statement: a recognisable plaintext marker, so a test can
# check that it does NOT appear in the ciphertext.
CONTENT = b"Transaction Date,Remarks,Withdrawal\r\n01/08/2026,UPI/ZOMATO/PAY,250.00\r\n"

# GCM appends a 16-byte authentication tag to the ciphertext.
TAG_BYTES = 16


def test_a_file_round_trips() -> None:
    encrypted = encrypt_file(CONTENT, UPLOAD_ID, key=KEY)

    assert decrypt_file(encrypted, UPLOAD_ID, key=KEY) == CONTENT


def test_the_ciphertext_does_not_contain_the_file() -> None:
    encrypted = encrypt_file(CONTENT, UPLOAD_ID, key=KEY)

    assert b"ZOMATO" not in encrypted.ciphertext
    assert CONTENT not in encrypted.ciphertext
    # Same length as the file plus the tag: GCM is a stream mode, no padding.
    assert len(encrypted.ciphertext) == len(CONTENT) + TAG_BYTES


def test_each_encryption_uses_a_fresh_nonce() -> None:
    """The same file encrypted twice gives two different results.

    A repeated nonce under one key is GCM's one catastrophic mistake, so
    this checks the nonce is random per call rather than fixed.
    """
    first = encrypt_file(CONTENT, UPLOAD_ID, key=KEY)
    second = encrypt_file(CONTENT, UPLOAD_ID, key=KEY)

    assert len(first.nonce) == NONCE_BYTES
    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext


def test_an_empty_file_round_trips() -> None:
    # Not a valid statement, but encryption has no opinion on that: the
    # parser refuses it later with a proper message.
    encrypted = encrypt_file(b"", UPLOAD_ID, key=KEY)

    assert decrypt_file(encrypted, UPLOAD_ID, key=KEY) == b""


# --- Decryption must fail -------------------------------------------------


@pytest.mark.parametrize("position", [0, len(CONTENT) // 2, len(CONTENT) + TAG_BYTES - 1])
def test_a_changed_byte_is_detected(position: int) -> None:
    """Integrity: flip one bit anywhere (start, middle, inside the tag)."""
    encrypted = encrypt_file(CONTENT, UPLOAD_ID, key=KEY)
    tampered = bytearray(encrypted.ciphertext)
    tampered[position] ^= 0x01

    with pytest.raises(FileDecryptionError):
        decrypt_file(EncryptedFile(encrypted.nonce, bytes(tampered)), UPLOAD_ID, key=KEY)


def test_a_changed_nonce_is_detected() -> None:
    encrypted = encrypt_file(CONTENT, UPLOAD_ID, key=KEY)
    other_nonce = bytes(byte ^ 0xFF for byte in encrypted.nonce)

    with pytest.raises(FileDecryptionError):
        decrypt_file(EncryptedFile(other_nonce, encrypted.ciphertext), UPLOAD_ID, key=KEY)


def test_a_blob_does_not_decrypt_for_another_upload() -> None:
    """The associated data: a blob moved onto another upload row is useless.

    This is what stops someone with write access to the database from
    attaching one user's statement to another user's upload.
    """
    encrypted = encrypt_file(CONTENT, UPLOAD_ID, key=KEY)

    with pytest.raises(FileDecryptionError):
        decrypt_file(encrypted, OTHER_UPLOAD_ID, key=KEY)


def test_the_wrong_key_is_detected() -> None:
    encrypted = encrypt_file(CONTENT, UPLOAD_ID, key=KEY)

    with pytest.raises(FileDecryptionError):
        decrypt_file(encrypted, UPLOAD_ID, key=OTHER_KEY)


@pytest.mark.parametrize("length", [0, 8, 11, 13, 16, 200])
def test_a_stored_nonce_of_the_wrong_length_is_a_decryption_error(length: int) -> None:
    """A damaged nonce is an unusable file, like any other bad blob.

    Without the explicit check, 0 or 200 bytes would make AESGCM raise
    ValueError (which the worker would treat as unexpected and retry),
    and 8 or 16 bytes would be accepted and only fail at the tag.
    """
    encrypted = encrypt_file(CONTENT, UPLOAD_ID, key=KEY)

    with pytest.raises(FileDecryptionError):
        decrypt_file(EncryptedFile(bytes(length), encrypted.ciphertext), UPLOAD_ID, key=KEY)


def test_the_error_message_carries_no_detail() -> None:
    encrypted = encrypt_file(CONTENT, UPLOAD_ID, key=KEY)

    with pytest.raises(FileDecryptionError) as excinfo:
        decrypt_file(encrypted, OTHER_UPLOAD_ID, key=KEY)

    assert str(excinfo.value) == "The stored file could not be decrypted."
    # `from None`: no chained cryptography exception for a traceback to show.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


# --- The key --------------------------------------------------------------


def test_the_settings_key_is_used_by_default() -> None:
    encrypted = encrypt_file(CONTENT, UPLOAD_ID)

    # Decrypting with the settings key given explicitly proves which key
    # the default was.
    assert decrypt_file(encrypted, UPLOAD_ID, key=get_settings().file_encryption_key_bytes) == (
        CONTENT
    )
    assert decrypt_file(encrypted, UPLOAD_ID) == CONTENT


@pytest.mark.parametrize("length", [16, 24, 31, 33])
def test_a_key_that_is_not_32_bytes_is_refused(length: int) -> None:
    """AES-128 and AES-192 keys (16/24 bytes) would work, silently.

    The library accepts them, so without this check a short key would quietly
    downgrade the encryption instead of failing.
    """
    with pytest.raises(ValueError, match="32 bytes"):
        encrypt_file(CONTENT, UPLOAD_ID, key=bytes(length))


def test_a_wrong_key_length_on_decrypt_is_not_disguised_as_a_bad_file() -> None:
    """A short key is the caller's bug, not damaged data: ValueError, not
    FileDecryptionError, so it is never mistaken for "this upload is broken"."""
    encrypted = encrypt_file(CONTENT, UPLOAD_ID, key=KEY)

    with pytest.raises(ValueError, match="32 bytes"):
        decrypt_file(encrypted, UPLOAD_ID, key=bytes(16))
