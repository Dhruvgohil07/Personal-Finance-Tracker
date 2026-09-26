"""Sanity checks on the seed data itself (no database needed)."""

import re

from app.db.seed_data import COMMON_MERCHANTS, SYSTEM_CATEGORIES

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")  # e.g. "food-dining"
KEY_PATTERN = re.compile(r"^[A-Z0-9]+$")  # e.g. "ZOMATO"
HEX_COLOR_PATTERN = re.compile(r"^#[0-9A-F]{6}$")


def test_category_slugs_are_unique() -> None:
    slugs = [c.slug for c in SYSTEM_CATEGORIES]
    assert len(slugs) == len(set(slugs))


def test_category_slugs_are_kebab_case() -> None:
    bad = [c.slug for c in SYSTEM_CATEGORIES if not SLUG_PATTERN.match(c.slug)]
    assert bad == []


def test_category_colors_are_hex() -> None:
    bad = [c.slug for c in SYSTEM_CATEGORIES if not HEX_COLOR_PATTERN.match(c.color)]
    assert bad == []


def test_categorizer_fallback_categories_exist() -> None:
    # The categorizer (SPEC §8) relies on these slugs existing.
    slugs = {c.slug for c in SYSTEM_CATEGORIES}
    assert {"uncategorized", "self-transfer", "transfers-to-people"} <= slugs


def test_merchant_keys_are_unique_and_uppercase() -> None:
    keys = [m.normalized_key for m in COMMON_MERCHANTS]
    assert len(keys) == len(set(keys))
    assert [k for k in keys if not KEY_PATTERN.match(k)] == []


def test_merchant_categories_exist() -> None:
    category_slugs = {c.slug for c in SYSTEM_CATEGORIES}

    bad_merchants = [
        m.normalized_key for m in COMMON_MERCHANTS if m.default_category_slug not in category_slugs
    ]
    assert bad_merchants == []
