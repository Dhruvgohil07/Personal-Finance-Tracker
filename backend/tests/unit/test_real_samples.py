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
"""

from collections import Counter
from pathlib import Path

import pytest

from app.parsers.base import ParsedStatement
from app.parsers.normalize import normalize_description
from app.parsers.readers import read_statement
from app.parsers.registry import build_default_registry

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
