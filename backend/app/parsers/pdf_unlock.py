"""Remove the password from a password-protected PDF statement (Phase 2 Step 10).

Indian banks commonly email statements as PDFs locked with a password
(often built from the customer's name and date of birth). The worker
cannot ask anybody for a password, and CLAUDE.md §7.2 forbids handing it
one: a job's arguments sit in Redis in plain text. So the lock comes off
HERE, during the HTTP request that carries the password (ADR 020):

    locked PDF + password  --unlock_pdf()-->  an unlocked copy of the PDF

The API then encrypts that copy with OUR key (AES-256-GCM, like every
stored upload) and the worker reads it as an ordinary PDF. The password is
only ever a local variable in this one request.

**Why pypdf.** pdfplumber (the reader) can open a locked PDF when given
the password, but it can only READ: it cannot save an unlocked copy, which
is what the worker needs. pypdf can do both: decrypt the file, then write
it out again without encryption. The copy is the same PDF - same pages,
same drawing instructions, so the same words at the same positions - and
the test suite checks that it parses to exactly the same statement.

**An empty password is always tried first.** A PDF can be encrypted with
only an OWNER password, which restricts printing or copying but lets anyone
open it (the "user" password is empty). Such a file needs nothing from the
user, so it is unlocked without asking.

Like the rest of `app/parsers/`, this module is pure: bytes in, bytes out,
no database, no web framework, no logging.
"""

import io

from pypdf import PasswordType, PdfReader, PdfWriter

from app.parsers.base import ParseError
from app.parsers.readers import PDF_UNREADABLE_MESSAGE


class PdfLockedError(ParseError):
    """The PDF is locked and no password that opens it was given."""


class WrongPdfPasswordError(PdfLockedError):
    """A password WAS given, and it does not open the PDF.

    A subclass of `PdfLockedError`, because the outcome is the same - the
    file is still locked - and a caller that does not care about the
    difference can catch just the parent. The upload service does care:
    a missing password asks for one, a wrong one is a 400.
    """


def unlock_pdf(content: bytes, password: str | None = None) -> bytes:
    """Return the PDF without its password protection.

    - Not encrypted: `content` itself, unchanged (and `password` ignored).
    - Encrypted, and the empty password or `password` opens it: an
      unlocked copy.
    - Encrypted, no password given: `PdfLockedError`.
    - Encrypted, `password` does not open it: `WrongPdfPasswordError`.

    **A PDF that pypdf cannot open or decrypt is returned unchanged too.**
    Deciding that a file is unreadable is the worker's job (ADR 013), and
    pdfplumber is more forgiving than pypdf with some files: a PDF pypdf
    refuses - a damaged one, or one with an encryption variant pypdf does
    not implement - may still import. If it really cannot be read, the
    worker reports that on the upload, as for any other bad file ("this
    PDF is password-protected and could not be unlocked" for a locked one).

    Either password works: the user password (the one that opens the
    file) or the owner password (the one that sets its permissions).

    One reader does all of it: the file is parsed once, and `decrypt` is
    simply called again with the user's password if the empty one fails.
    `decrypt` returns which password matched: USER_PASSWORD,
    OWNER_PASSWORD, or NOT_DECRYPTED when neither did.

    The password is never put into an exception or anywhere else, and no
    pypdf exception travels along with ours: the ones raised while
    handling a pypdf error are raised `from None`, which drops it.
    """
    try:
        reader = PdfReader(io.BytesIO(content))
        if not reader.is_encrypted:
            return content
        matched = reader.decrypt("")
        if matched is PasswordType.NOT_DECRYPTED and password:
            matched = reader.decrypt(password)
    except Exception:
        # Untrusted bytes, and pypdf raises many kinds of errors on a
        # damaged or unusual file (PdfReadError, PdfStreamError,
        # NotImplementedError, its own LimitReachedError...). Not ours to
        # judge; see the docstring.
        return content

    if matched is PasswordType.NOT_DECRYPTED:
        if password:
            raise WrongPdfPasswordError("the password does not open this PDF")
        raise PdfLockedError("this PDF is password-protected")
    return _unlocked_copy(reader)


def _unlocked_copy(reader: PdfReader) -> bytes:
    """Write the decrypted document out again, this time without encryption.

    `clone_from` copies every page and object of the decrypted reader into
    a new writer. A writer only encrypts when told to (`writer.encrypt`),
    so the bytes it writes are a plain PDF.
    """
    try:
        writer = PdfWriter(clone_from=reader)
        output = io.BytesIO()
        writer.write(output)
    except Exception:
        raise ParseError(PDF_UNREADABLE_MESSAGE) from None
    return output.getvalue()
