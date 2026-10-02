"""Tests for app.parsers.registry: choosing a parser for a file.

The parsers here are tiny fakes. That is the advantage of the Protocol in
`base.py`: a test parser only has to *look* like a `StatementParser`, so
these tests do not depend on any real bank parser (Step 6) existing yet.
"""

import pytest

from app.parsers.base import FileType, ParsedStatement, ParserInput
from app.parsers.registry import DETECTION_THRESHOLD, ParserRegistry


class FakeParser:
    """A parser that reports a fixed confidence."""

    def __init__(
        self,
        bank_code: str,
        score: float,
        file_types: tuple[FileType, ...] = (FileType.CSV,),
    ) -> None:
        self.bank_code = bank_code
        self.file_types = file_types
        self._score = score
        self.detect_calls = 0

    def detect(self, data: ParserInput) -> float:
        self.detect_calls += 1
        return self._score

    def parse(self, data: ParserInput) -> ParsedStatement:
        return ParsedStatement()


CSV_FILE = ParserInput(file_type=FileType.CSV, rows=[["Date", "Narration", "Amount"]])
XLS_FILE = ParserInput(file_type=FileType.XLS, rows=[["Date", "Narration", "Amount"]])
PDF_FILE = ParserInput(file_type=FileType.PDF, rows=[["Date", "Narration", "Amount"]])


# --- register -------------------------------------------------------------


def test_parsers_are_kept_in_registration_order() -> None:
    registry = ParserRegistry()
    first, second = FakeParser("ICICI", 0.9), FakeParser("HDFC", 0.8)

    registry.register(first)
    registry.register(second)

    assert registry.parsers == (first, second)


def test_the_parsers_property_cannot_be_used_to_add_a_parser() -> None:
    registry = ParserRegistry()
    registry.register(FakeParser("ICICI", 0.9))

    # A tuple copy, so a caller cannot append to the registry's own list.
    assert isinstance(registry.parsers, tuple)


def test_a_parser_without_a_bank_code_is_rejected() -> None:
    with pytest.raises(ValueError, match="bank_code"):
        ParserRegistry().register(FakeParser("", 0.9))


def test_a_parser_without_file_types_is_rejected() -> None:
    # Otherwise it would be registered and then silently never chosen.
    with pytest.raises(ValueError, match="file_types"):
        ParserRegistry().register(FakeParser("ICICI", 0.9, file_types=()))


def test_the_same_bank_and_file_type_twice_is_rejected() -> None:
    registry = ParserRegistry()
    registry.register(FakeParser("ICICI", 0.9, file_types=(FileType.XLS,)))

    with pytest.raises(ValueError, match="already registered"):
        registry.register(FakeParser("ICICI", 0.8, file_types=(FileType.XLS, FileType.CSV)))


def test_one_bank_may_have_one_parser_per_file_type() -> None:
    # HDFC gives CSVs and PDFs; those are two different layouts, so two
    # parsers for the same bank are expected.
    registry = ParserRegistry()
    registry.register(FakeParser("HDFC", 0.9, file_types=(FileType.CSV,)))
    registry.register(FakeParser("HDFC", 0.9, file_types=(FileType.PDF,)))

    assert len(registry.parsers) == 2


# --- detect ---------------------------------------------------------------


def test_the_most_confident_parser_wins() -> None:
    registry = ParserRegistry()
    unsure, confident = FakeParser("HDFC", 0.6), FakeParser("ICICI", 0.95)
    registry.register(unsure)
    registry.register(confident)

    assert registry.detect(CSV_FILE) is confident


def test_no_parser_above_the_threshold_gives_none() -> None:
    # Step 7 turns this into "use the user's column mapping, or reject the
    # file" - the registry only reports that nothing recognised it.
    registry = ParserRegistry()
    registry.register(FakeParser("HDFC", 0.2))
    registry.register(FakeParser("ICICI", 0.49))

    assert registry.detect(CSV_FILE) is None


def test_a_score_exactly_at_the_threshold_is_accepted() -> None:
    registry = ParserRegistry()
    parser = FakeParser("ICICI", DETECTION_THRESHOLD)
    registry.register(parser)

    assert registry.detect(CSV_FILE) is parser


def test_the_threshold_can_be_overridden_per_call() -> None:
    registry = ParserRegistry()
    parser = FakeParser("ICICI", 0.4)
    registry.register(parser)

    assert registry.detect(CSV_FILE) is None
    assert registry.detect(CSV_FILE, threshold=0.3) is parser


def test_a_tie_goes_to_the_parser_registered_first() -> None:
    # Deterministic on purpose: an outcome that depended on set or dict
    # ordering would be impossible to test or to explain.
    registry = ParserRegistry()
    first, second = FakeParser("ICICI", 0.9), FakeParser("HDFC", 0.9)
    registry.register(first)
    registry.register(second)

    assert registry.detect(CSV_FILE) is first


def test_parsers_for_other_file_types_are_not_consulted() -> None:
    registry = ParserRegistry()
    csv_parser = FakeParser("HDFC", 0.9, file_types=(FileType.CSV,))
    xls_parser = FakeParser("ICICI", 0.7, file_types=(FileType.XLS,))
    registry.register(csv_parser)
    registry.register(xls_parser)

    assert registry.detect(XLS_FILE) is xls_parser
    # The CSV parser was never even asked about the .xls file.
    assert csv_parser.detect_calls == 0


def test_a_file_type_nobody_handles_gives_none() -> None:
    registry = ParserRegistry()
    registry.register(FakeParser("ICICI", 0.9, file_types=(FileType.XLS,)))

    assert registry.detect(PDF_FILE) is None


def test_an_empty_registry_gives_none() -> None:
    assert ParserRegistry().detect(CSV_FILE) is None


def test_each_parser_is_scored_only_once_per_call() -> None:
    registry = ParserRegistry()
    parser = FakeParser("ICICI", 0.9)
    registry.register(parser)

    registry.detect(CSV_FILE)

    # detect() is cheap but not free (it reads header cells); the winner
    # must not be re-scored for the threshold comparison.
    assert parser.detect_calls == 1


def test_a_parser_whose_detect_raises_is_not_swallowed() -> None:
    class BrokenParser(FakeParser):
        def detect(self, data: ParserInput) -> float:
            raise RuntimeError("bug in detect")

    registry = ParserRegistry()
    registry.register(BrokenParser("ICICI", 0.9))

    # A bug in a parser should fail loudly, not look like "unrecognised file".
    with pytest.raises(RuntimeError, match="bug in detect"):
        registry.detect(CSV_FILE)
