"""User rules without a database (Phase 2 Step 7, ADR 017).

- `normalize_rule_pattern`: a pattern is stored in the form its field has
- `learned_rule_key`: what a correction's rule matches on
- the request schemas: `RuleCreate`, `RuleUpdate`, and `apply_to_similar`

The end-to-end behaviour (CRUD, IDOR, the re-categorization job) is in
tests/integration/test_rules.py. All names and VPAs are invented.
"""

import uuid

import pytest
from pydantic import ValidationError

from app.categorizer.handlers import (
    RuleMatchType,
    looks_like_person,
    normalize_rule_pattern,
)
from app.categorizer.learning import learned_rule_key
from app.models.category_rule import DEFAULT_RULE_PRIORITY
from app.parsers.base import Channel
from app.schemas.rule import RuleCreate, RuleUpdate
from app.schemas.transaction import TransactionUpdate
from tests.unit.test_categorizer import facts, upi

M, V, C = RuleMatchType.MERCHANT, RuleMatchType.VPA, RuleMatchType.CONTAINS

# --- normalize_rule_pattern -----------------------------------------------------


@pytest.mark.parametrize(
    ("match_type", "typed", "stored"),
    [
        # The same function that built the stored merchant keys.
        (M, "Moon Cafe", "MOONCAFE"),
        (M, "mooncafe", "MOONCAFE"),
        (M, "Zomato Ltd", "ZOMATO"),
        # Stored VPAs are lowercase, and may be cut before the handle.
        (V, "  Asha.Verma@OKAXIS ", "asha.verma@okaxis"),
        (V, "gpay-123456789", "gpay-123456789"),
        # The same function that built the stored narrations.
        (C, "  moon   cafe ", "MOON CAFE"),
        (C, "rent|flat 4", "RENT FLAT 4"),
        (C, "store 12345", "STORE 12345"),  # 5 digits survive normalization
    ],
)
def test_a_pattern_is_stored_as_its_field_is(
    match_type: RuleMatchType, typed: str, stored: str
) -> None:
    assert normalize_rule_pattern(match_type, typed) == stored


@pytest.mark.parametrize(
    ("match_type", "typed", "message"),
    [
        (M, "AB", "at least 3 letters or digits"),
        (M, "9876543210", "not only digits"),  # a phone number is not a name
        (M, "!!!", "at least 3"),
        (V, "ab", "at least 3 characters"),
        (V, "asha verma@okaxis", "letters, digits"),  # a space is never in a VPA
        (V, "asha#1@okaxis", "letters, digits"),
        (C, "ab", "at least 3 characters"),
        # A long number is refused rather than dropped: dropping it would
        # leave "UPI", which matches every UPI row (review of Step 7).
        (C, "upi 9876543210", "6 or more digits"),
        (C, "123456789", "6 or more digits"),
        (C, "  !! ", "at least 3 characters"),
    ],
)
def test_a_pattern_that_would_match_nothing_or_everything_is_refused(
    match_type: RuleMatchType, typed: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_rule_pattern(match_type, typed)


# --- learned_rule_key -------------------------------------------------------------


def test_a_person_is_learned_by_their_vpa() -> None:
    """Two RAVI KUMARs share a cut name, never a VPA."""
    row = upi("RAVIKUMAR", "ravi.kumar@ok")

    assert looks_like_person(row)
    assert learned_rule_key(row) == (V, "ravi.kumar@ok")


def test_a_shop_is_learned_by_its_merchant_key() -> None:
    """A QR code is one counter of the shop; the key covers all of them."""
    row = upi("MOONCAFE", "q123456789@ybl")

    assert not looks_like_person(row)
    assert learned_rule_key(row) == (M, "MOONCAFE")


def test_a_known_merchant_is_learned_by_its_key_although_its_vpa_looks_personal() -> None:
    row = upi("SWIGGYLIM", "swiggypayu@hdf", dictionary_key="SWIGGY")

    assert looks_like_person(row)  # the VPA's shape alone says "person"
    assert learned_rule_key(row) == (M, "SWIGGYLIM")


def test_a_business_word_in_the_name_means_merchant_key() -> None:
    row = upi("SHARMASTORE", "sharma@okicici", name="SHARMA STORE")

    assert learned_rule_key(row) == (M, "SHARMASTORE")


def test_a_non_upi_row_is_learned_by_its_merchant_key() -> None:
    row = facts("NEFT CR-ACME", channel=Channel.NEFT, merchant_key="ACME")

    assert learned_rule_key(row) == (M, "ACME")


def test_a_row_with_neither_has_nothing_to_learn() -> None:
    # A cash withdrawal names nobody.
    assert learned_rule_key(facts("NFS/CASH WDL", channel=Channel.ATM)) is None
    assert learned_rule_key(facts("X", merchant_key="")) is None


# --- RuleCreate / RuleUpdate --------------------------------------------------------


def test_rule_create_normalizes_the_pattern_for_its_type() -> None:
    category = uuid.uuid4()

    body = RuleCreate.model_validate(
        {"match_type": "merchant", "pattern": " Moon Cafe ", "category_id": str(category)}
    )

    assert (body.match_type, body.pattern, body.category_id) == (M, "MOONCAFE", category)
    assert body.priority == DEFAULT_RULE_PRIORITY


@pytest.mark.parametrize(
    "body",
    [
        {"match_type": "vpa", "pattern": "a b", "category_id": str(uuid.uuid4())},
        {"match_type": "merchant", "pattern": "", "category_id": str(uuid.uuid4())},
        {"match_type": "merchant", "pattern": "x" * 101, "category_id": str(uuid.uuid4())},
        {"match_type": "merchant", "pattern": "moon\ncafe", "category_id": str(uuid.uuid4())},
        {"match_type": "contains", "pattern": "rent", "category_id": "nope"},
        {"match_type": "merchant", "pattern": "moon", "category_id": str(uuid.uuid4()), "x": 1},
        {"match_type": "vpa", "pattern": "a@b", "category_id": str(uuid.uuid4()), "priority": -1},
        {"match_type": "vpa", "pattern": "a@b", "category_id": str(uuid.uuid4()), "priority": 1001},
    ],
)
def test_rule_create_rejects(body: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RuleCreate.model_validate(body)


def test_an_unknown_match_type_is_one_error_not_two() -> None:
    """The pattern validator skips when the match type already failed."""
    with pytest.raises(ValidationError) as excinfo:
        RuleCreate.model_validate(
            {"match_type": "regex", "pattern": "x", "category_id": str(uuid.uuid4())}
        )

    assert [error["loc"] for error in excinfo.value.errors()] == [("match_type",)]


@pytest.mark.parametrize("field", ["category_id", "priority"])
def test_rule_update_may_leave_a_field_out_but_not_null_it(field: str) -> None:
    assert RuleUpdate.model_validate({}).model_dump(exclude_unset=True) == {}
    with pytest.raises(ValidationError, match="not set to null"):
        RuleUpdate.model_validate({field: None})


def test_rule_update_cannot_change_what_a_rule_matches() -> None:
    with pytest.raises(ValidationError):
        RuleUpdate.model_validate({"pattern": "SUNCAFE"})


# --- apply_to_similar ----------------------------------------------------------------


def test_apply_to_similar_needs_a_category() -> None:
    for body in ({"apply_to_similar": True}, {"apply_to_similar": True, "category_id": None}):
        with pytest.raises(ValidationError, match="apply_to_similar needs a category_id"):
            TransactionUpdate.model_validate(body)


def test_apply_to_similar_is_off_unless_sent() -> None:
    category = str(uuid.uuid4())

    assert TransactionUpdate.model_validate({"category_id": category}).apply_to_similar is False
    body = TransactionUpdate.model_validate({"category_id": category, "apply_to_similar": True})
    assert body.apply_to_similar is True
