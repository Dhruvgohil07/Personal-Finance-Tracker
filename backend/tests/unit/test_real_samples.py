"""The real-file cross-check of the PDF parser (Phase 2 Step 9, ADR 019).

tests/integration/test_pdf_imports.py proves the `.xls` and PDF parsers
agree on SYNTHETIC files - but its PDFs are drawn by our own fixture, which
wraps text the way the parser assumes, so it cannot show that the parser
matches the BANK (review of Step 9). Only real statements can.

This test reads the real statements in `samples/` (never committed,
CLAUDE.md rule 8) and, for every `.xls` and PDF that cover a common day,
checks that on those days both give exactly the same transactions - date,
amount, direction, balance and normalized narration: what the fingerprint
is built from (ADR 006).

It runs only on a machine whose `samples/` holds such a pair, and is
skipped everywhere else, CI included (decided with Dhruv). Every assertion
compares COUNTS, so a failure prints how many rows differ, never what they
contain (CLAUDE.md rule 3).

Step 10 adds a second real-file check: every real PDF, locked in memory
with a test password and unlocked again by `unlock_pdf`, must read exactly
like the original (ADR 020). No real password-protected statement is in
`samples/`, so the bank's own layout is what this proves survives the
rewrite; the locking is ours.

The review of Step 11 adds a third: every real statement must still
auto-categorize MORE THAN HALF of its rows without an LLM, the Phase 2
"Done when" bar (docs/phase-2-acceptance.md). The August `.xls` cleared it
by two rows, so a change to the chain could quietly break it; this test
re-measures it from the pure chain on every local run.
"""

from collections import Counter
from pathlib import Path

import pytest

from app.categorizer.base import CategorySource, TxnFacts, categorize
from app.categorizer.handlers import build_chain
from app.db.seed_data import COMMON_MERCHANTS
from app.parsers.base import ParsedStatement
from app.parsers.narration import counterparty_name, dictionary_key
from app.parsers.normalize import normalize_description, normalize_row
from app.parsers.pdf_unlock import unlock_pdf
from app.parsers.readers import read_pdf, read_statement
from app.parsers.registry import build_default_registry
from tests.fixtures.icici_pdf import encrypt_pdf

SAMPLES = Path(__file__).resolve().parents[3] / "samples"


def _statements(suffix: str) -> list[ParsedStatement]:
    if not SAMPLES.is_dir():
        return []
    registry = build_default_registry()
    parsed = []
    for path in sorted(SAMPLES.iterdir()):
        if path.suffix.lower() != suffix:
            continue
        data = read_statement(path.name, path.read_bytes())
        parser = registry.detect(data)
        if parser is not None:
            parsed.append(parser.parse(data))
    return parsed


def _keys(statement: ParsedStatement, days: set[object]) -> Counter[tuple[object, ...]]:
    return Counter(
        (
            row.txn_date,
            row.amount_paise,
            row.direction,
            row.balance_after_paise,
            normalize_description(row.raw_description),
        )
        for row in statement.rows
        if row.txn_date in days
    )


def _pairs() -> list[tuple[ParsedStatement, ParsedStatement, set[object]]]:
    pairs = []
    for xls in _statements(".xls"):
        for pdf in _statements(".pdf"):
            shared = {row.txn_date for row in xls.rows} & {row.txn_date for row in pdf.rows}
            if shared:
                pairs.append((xls, pdf, shared))
    return pairs


def test_a_real_xls_and_pdf_of_the_same_days_give_the_same_rows() -> None:
    pairs = _pairs()
    if not pairs:
        pytest.skip("samples/ holds no .xls and PDF statement that share a day")

    for xls, pdf, shared in pairs:
        from_xls, from_pdf = _keys(xls, shared), _keys(pdf, shared)
        only_in_xls = sum((from_xls - from_pdf).values())
        only_in_pdf = sum((from_pdf - from_xls).values())
        # Counts only - see the module docstring.
        assert (only_in_xls, only_in_pdf) == (0, 0), (
            f"on {len(shared)} shared days: {only_in_xls} of {sum(from_xls.values())} .xls "
            f"rows and {only_in_pdf} of {sum(from_pdf.values())} PDF rows have no match"
        )
        assert xls.account_last4 == pdf.account_last4, "the two files name different accounts"


@pytest.mark.parametrize("algorithm", ["RC4-128", "AES-128", "AES-256"])
def test_a_real_pdf_locked_and_unlocked_reads_exactly_the_same(algorithm: str) -> None:
    pdfs = sorted(SAMPLES.glob("*.pdf")) if SAMPLES.is_dir() else []
    if not pdfs:
        pytest.skip("samples/ holds no PDF statement")

    for path in pdfs:
        original = read_pdf(path.read_bytes())
        locked = encrypt_pdf(path.read_bytes(), user_password="Test-0101", algorithm=algorithm)

        unlocked = read_pdf(unlock_pdf(locked, "Test-0101"))

        # Counts only - see the module docstring. `==` on the dataclass
        # compares every word's text, page and position. strict=False: on a
        # failure the two lists may differ in length, and the message
        # reports both lengths.
        same_words = sum(
            a == b for a, b in zip(original.pdf_words, unlocked.pdf_words, strict=False)
        )
        assert unlocked == original, (
            f"{same_words} of {len(original.pdf_words)} words unchanged "
            f"({len(unlocked.pdf_words)} after unlocking)"
        )


def test_more_than_half_of_each_real_statement_categorizes_without_an_llm() -> None:
    """The same chain an import runs, with the seeded merchant dictionary and
    no user rules (a new user's view). Self-transfer detection needs two
    accounts and is left out: it can only add categorized rows."""
    statements = _statements(".xls") + _statements(".pdf")
    if not statements:
        pytest.skip("samples/ holds no statement")
    merchants = {m.normalized_key: m.default_category_slug for m in COMMON_MERCHANTS}
    chain = build_chain(rules=[], merchant_categories=merchants)

    for statement in statements:
        rows = [normalize_row(row) for row in statement.rows]
        decided = sum(
            categorize(
                TxnFacts(
                    direction=row.raw.direction,
                    channel=row.channel,
                    normalized_description=row.normalized_description,
                    merchant_key=row.merchant_key,
                    counterparty_vpa=row.counterparty_vpa,
                    dictionary_key=dictionary_key(row.merchant_key, merchants.keys()),
                    counterparty_name=counterparty_name(row.raw.raw_description),
                    category_source=CategorySource.NONE,
                ),
                chain,
            )
            is not None
            for row in rows
        )
        # Counts only - see the module docstring.
        assert decided * 2 > len(rows), f"only {decided} of {len(rows)} rows categorized"
