"""Every model's `__repr__` is safe to put in a log (CLAUDE.md rule 3).

A repr is what Python shows for an object in a traceback, a debugger, a
`print()` or a structlog event - all places whose output we keep. So a repr
may carry IDs and non-personal labels, and nothing else: no email, no
password hash, no account number, no nickname, no token hash.

Two models are missing here on purpose: `Transaction` and `StatementUpload`
are checked in tests/integration/test_uploads.py, against rows a real
import produced (the strongest version of this test, since the sensitive
values there are the ones the parser actually read).

No database is needed. A model is an ordinary Python object until
`db.add()`, so these are built in memory - with `id` passed explicitly,
because the ORM only fills its `default=uuid.uuid4` when the row is
flushed.
"""

import uuid
from datetime import UTC, datetime

import pytest

from app.categorizer.handlers import RuleMatchType
from app.db.base import Base
from app.models import (
    Account,
    AccountType,
    BankCode,
    Category,
    CategoryKind,
    CategoryRule,
    Merchant,
    RefreshToken,
    UploadBlob,
    User,
)

# Fixed ids, so the expected repr can be written out in full. None of them
# contains "4321" or any other sensitive value below as a substring, which
# would make the leak assertions pass for the wrong reason.
USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
ACCOUNT_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
CATEGORY_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
MERCHANT_ID = uuid.UUID("66666666-6666-4666-8666-666666666666")
TOKEN_ID = uuid.UUID("77777777-7777-4777-8777-777777777777")
FAMILY_ID = uuid.UUID("88888888-8888-4888-8888-888888888888")
UPLOAD_ID = uuid.UUID("99999999-9999-4999-8999-999999999999")
RULE_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

# The values that must never show up in a repr.
EMAIL = "dhruv@example.com"
PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2g"
HOLDER_NAME = "Dhruv Gohil"
NICKNAME = "Salary account"
MASKED_NUMBER = "4321"
TOKEN_HASH = "9e107d9d372bb6826bd81d3542a419d6" * 2
# A blob's bytes. Encrypted in real life, but a repr must not show them in
# any form, so the test uses readable ones that would be easy to spot.
BLOB_CONTENT = b"UPI/RAMESHKUMAR/PAY 250.00"
VPA = "ramesh.kumar@okaxis"
SENSITIVE = (
    VPA,
    EMAIL,
    PASSWORD_HASH,
    HOLDER_NAME,
    NICKNAME,
    MASKED_NUMBER,
    TOKEN_HASH,
    "RAMESHKUMAR",
    str(BLOB_CONTENT),
)


def make_user() -> User:
    return User(id=USER_ID, email=EMAIL, password_hash=PASSWORD_HASH, name=HOLDER_NAME)


def make_account() -> Account:
    return Account(
        id=ACCOUNT_ID,
        user_id=USER_ID,
        bank_code=BankCode.ICICI,
        nickname=NICKNAME,
        account_type=AccountType.SAVINGS,
        masked_number=MASKED_NUMBER,
    )


def make_system_category() -> Category:
    return Category(id=CATEGORY_ID, name="Groceries", slug="groceries", kind=CategoryKind.EXPENSE)


def make_user_category() -> Category:
    return Category(
        id=CATEGORY_ID,
        user_id=USER_ID,
        name="Cricket kit",
        slug="cricket-kit",
        kind=CategoryKind.EXPENSE,
    )


def make_merchant() -> Merchant:
    return Merchant(
        id=MERCHANT_ID,
        normalized_key="ZOMATO",
        display_name="Zomato",
        default_category_slug="food-dining",
    )


def make_refresh_token() -> RefreshToken:
    return RefreshToken(
        id=TOKEN_ID,
        user_id=USER_ID,
        token_hash=TOKEN_HASH,
        family_id=FAMILY_ID,
        expires_at=datetime(2026, 11, 12, tzinfo=UTC),
        revoked_at=None,
    )


def make_upload_blob() -> UploadBlob:
    return UploadBlob(upload_id=UPLOAD_ID, encrypted_content=BLOB_CONTENT, nonce=bytes(12))


def make_category_rule() -> CategoryRule:
    # A `vpa` rule: its pattern is a person's UPI id.
    return CategoryRule(
        id=RULE_ID,
        user_id=USER_ID,
        match_type=RuleMatchType.VPA,
        pattern=VPA,
        category_id=CATEGORY_ID,
    )


# --- The exact text of each repr ------------------------------------------


def test_user_repr_is_the_id_alone() -> None:
    # Deliberately NOT the email: it identifies a person all by itself.
    assert repr(make_user()) == f"User(id={USER_ID})"


def test_account_repr_is_ids_alone() -> None:
    assert repr(make_account()) == f"Account(id={ACCOUNT_ID}, user_id={USER_ID})"


def test_refresh_token_repr_is_ids_alone() -> None:
    assert repr(make_refresh_token()) == f"RefreshToken(id={TOKEN_ID}, family_id={FAMILY_ID})"


def test_category_repr_names_the_slug_and_owner() -> None:
    # A slug is our own vocabulary, not personal data, and it is how the
    # rest of the code refers to a category - so it is the useful thing to
    # see in a log. user_id says whether this is a system row (None) or
    # someone's own category.
    assert repr(make_system_category()) == "Category(slug='groceries', user_id=None)"
    assert repr(make_user_category()) == f"Category(slug='cricket-kit', user_id={USER_ID})"


def test_merchant_repr_names_the_key() -> None:
    # The merchants table is shared by all users and must never hold
    # personal data (see the model's docstring), so its key is safe to log.
    assert repr(make_merchant()) == "Merchant(normalized_key='ZOMATO')"


def test_upload_blob_repr_is_the_upload_id_alone() -> None:
    assert repr(make_upload_blob()) == f"UploadBlob(upload_id={UPLOAD_ID})"


def test_category_rule_repr_is_the_id_and_match_type() -> None:
    # Not the pattern: for a `vpa` rule it identifies a person.
    assert repr(make_category_rule()) == f"CategoryRule(id={RULE_ID}, match_type='vpa')"


# --- The rule itself ------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        make_user(),
        make_account(),
        make_system_category(),
        make_user_category(),
        make_merchant(),
        make_refresh_token(),
        make_upload_blob(),
        make_category_rule(),
    ],
    ids=[
        "user",
        "account",
        "system_category",
        "user_category",
        "merchant",
        "refresh_token",
        "upload_blob",
        "category_rule",
    ],
)
def test_no_repr_leaks_a_sensitive_value(model: Base) -> None:
    text = repr(model)
    for value in SENSITIVE:
        assert value not in text
