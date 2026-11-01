"""`unlock_pdf`: taking the password off a PDF statement (Phase 2 Step 10, ADR 020).

Synthetic statements only (CLAUDE.md rule 8): `build_icici_pdf` draws one,
and `encrypt_pdf` locks it with each encryption method a bank might use.
The test that matters most is `test_the_unlocked_copy_reads_exactly_like_the_original`:
unlocking must not change a single word or position, or the worker would
import a different statement than the one the bank sent.
"""

from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader, PdfWriter

from app.parsers.base import ParseError
from app.parsers.pdf_unlock import PdfLockedError, WrongPdfPasswordError, unlock_pdf
from app.parsers.readers import PDF_UNREADABLE_MESSAGE, read_pdf
from tests.fixtures.icici_pdf import build_icici_pdf, encrypt_pdf, is_encrypted

PASSWORD = "dhru0101"
# Every method pypdf can write; banks use RC4 and AES of various strengths.
ALGORITHMS = ("RC4-40", "RC4-128", "AES-128", "AES-256-R5", "AES-256")


@pytest.fixture
def plain(tmp_path: Path) -> bytes:
    """An ordinary, unlocked synthetic ICICI statement."""
    return build_icici_pdf(tmp_path / "statement.pdf").read_bytes()


# --- Files that need nothing --------------------------------------------------


def test_a_pdf_without_a_password_is_returned_unchanged(plain: bytes) -> None:
    assert unlock_pdf(plain) is plain


def test_a_password_sent_with_an_unlocked_pdf_is_ignored(plain: bytes) -> None:
    assert unlock_pdf(plain, PASSWORD) is plain


@pytest.mark.parametrize(
    "content",
    [b"%PDF-1.4\nnot really a pdf", b"%PDF-"],
    ids=["garbage_after_signature", "signature_only"],
)
def test_a_pdf_pypdf_cannot_open_is_left_for_the_worker(content: bytes) -> None:
    """Judging a damaged file is the worker's job (ADR 013); see `unlock_pdf`."""
    assert unlock_pdf(content, PASSWORD) is content


def test_a_truncated_pdf_is_left_for_the_worker(plain: bytes) -> None:
    damaged = plain[: len(plain) // 2]

    assert unlock_pdf(damaged) is damaged


def test_a_pdf_with_only_an_owner_password_opens_without_asking(plain: bytes) -> None:
    """Empty user password: anyone may open it, so the user is not asked."""
    locked = encrypt_pdf(plain, user_password="")
    assert is_encrypted(locked)

    unlocked = unlock_pdf(locked)

    assert not is_encrypted(unlocked)
    assert read_pdf(unlocked) == read_pdf(plain)


# --- Locked files ------------------------------------------------------------------


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_the_password_unlocks_every_encryption_method(plain: bytes, algorithm: str) -> None:
    locked = encrypt_pdf(plain, user_password=PASSWORD, algorithm=algorithm)

    unlocked = unlock_pdf(locked, PASSWORD)

    assert not is_encrypted(unlocked)


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_the_unlocked_copy_reads_exactly_like_the_original(plain: bytes, algorithm: str) -> None:
    """Same lines, same words, same positions: the same statement.

    `ParserInput` is a frozen dataclass, so `==` compares every row and
    every `PdfWord` (text, page and all four coordinates).
    """
    locked = encrypt_pdf(plain, user_password=PASSWORD, algorithm=algorithm)

    assert read_pdf(unlock_pdf(locked, PASSWORD)) == read_pdf(plain)


def test_a_pdf_locked_by_another_writer_unlocks_too(tmp_path: Path, plain: bytes) -> None:
    """fpdf2's encryption, not pypdf's: not just pypdf agreeing with itself."""
    locked = build_icici_pdf(tmp_path / "locked.pdf", password=PASSWORD).read_bytes()
    assert is_encrypted(locked)

    assert read_pdf(unlock_pdf(locked, PASSWORD)) == read_pdf(plain)


def test_the_owner_password_unlocks_it_too(plain: bytes) -> None:
    locked = encrypt_pdf(plain, user_password=PASSWORD, owner_password="bank-owner")

    assert not is_encrypted(unlock_pdf(locked, "bank-owner"))


@pytest.mark.parametrize("password", [None, ""], ids=["none", "empty"])
def test_a_locked_pdf_without_a_password_asks_for_one(plain: bytes, password: str | None) -> None:
    locked = encrypt_pdf(plain, user_password=PASSWORD)

    with pytest.raises(PdfLockedError) as raised:
        unlock_pdf(locked, password)

    # "Asks for one", not "that one was wrong": the service tells these apart.
    assert type(raised.value) is PdfLockedError


def test_a_wrong_password_is_told_apart(plain: bytes) -> None:
    locked = encrypt_pdf(plain, user_password=PASSWORD)

    with pytest.raises(WrongPdfPasswordError) as raised:
        unlock_pdf(locked, "not-it")

    assert "not-it" not in str(raised.value)
    # Nothing chained either: the exception carries no pypdf internals.
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_a_password_differing_only_in_case_is_wrong(plain: bytes) -> None:
    """No guessing on our side: the password is used exactly as typed."""
    locked = encrypt_pdf(plain, user_password=PASSWORD)

    with pytest.raises(WrongPdfPasswordError):
        unlock_pdf(locked, PASSWORD.upper())


def test_the_unlocked_copy_does_not_contain_the_password(plain: bytes) -> None:
    locked = encrypt_pdf(plain, user_password=PASSWORD)

    assert PASSWORD.encode() not in unlock_pdf(locked, PASSWORD)


# --- pypdf failing half-way ----------------------------------------------------------


@pytest.mark.parametrize("password", [None, PASSWORD], ids=["no_password", "with_password"])
def test_an_encryption_pypdf_cannot_decrypt_is_left_for_the_worker(
    plain: bytes, monkeypatch: pytest.MonkeyPatch, password: str | None
) -> None:
    """`decrypt` raising (a variant pypdf does not implement, a damaged
    encryption dictionary) is not a wrong password, and not ours to judge
    either: the file goes on unchanged, and pdfplumber gets its chance in
    the worker (review of Step 10)."""
    locked = encrypt_pdf(plain, user_password=PASSWORD)

    def broken_decrypt(self: PdfReader, password: str) -> None:
        raise NotImplementedError("unsupported encryption")

    monkeypatch.setattr(PdfReader, "decrypt", broken_decrypt)

    assert unlock_pdf(locked, password) is locked


def test_the_empty_password_is_tried_once_and_the_file_parsed_once(
    plain: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One reader, two `decrypt` calls: "" first, then the user's password
    (review of Step 10: it used to parse the file three times)."""
    locked = encrypt_pdf(plain, user_password=PASSWORD)
    readers_built: list[object] = []
    attempts: list[str] = []
    real_init, real_decrypt = PdfReader.__init__, PdfReader.decrypt

    def counting_init(self: PdfReader, *args: Any, **kwargs: Any) -> None:
        readers_built.append(self)
        real_init(self, *args, **kwargs)

    def recording_decrypt(self: PdfReader, password: str) -> object:
        attempts.append("empty" if password == "" else "given")
        return real_decrypt(self, password)

    monkeypatch.setattr(PdfReader, "__init__", counting_init)
    monkeypatch.setattr(PdfReader, "decrypt", recording_decrypt)

    unlock_pdf(locked, PASSWORD)

    assert len(readers_built) == 1
    assert attempts == ["empty", "given"]


def test_a_copy_that_cannot_be_written_is_unreadable(
    plain: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    locked = encrypt_pdf(plain, user_password=PASSWORD)

    def broken_write(self: PdfWriter, stream: object) -> None:
        raise ValueError("cannot write")

    monkeypatch.setattr(PdfWriter, "write", broken_write)

    with pytest.raises(ParseError, match=f"^{PDF_UNREADABLE_MESSAGE}$"):
        unlock_pdf(locked, PASSWORD)
