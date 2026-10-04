"""Pick the right parser for an uploaded file (SPEC §6.2, Registry pattern).

The user uploads a file and tells us which *account* it belongs to, but not
which layout it has. Rather than a growing `if bank == "ICICI": ...` chain,
every parser answers one question about the file - "how sure are you that
this is yours?" - and the registry picks the most confident one.

    registry = ParserRegistry()
    registry.register(IciciXlsParser())
    registry.register(HdfcCsvParser())

    parser = registry.detect(data)
    if parser is None:
        ...   # fall back to the generic CSV parser, see below
    statement = parser.parse(data)

Adding a bank is then: write the parser, register it. No existing file
changes, which is the point of the pattern (open for extension, closed for
modification).

Why doesn't the registry fall back to `GenericCsvParser` itself, as SPEC
§6.2 describes? Because that parser cannot work from the file alone - it
needs the column mapping the user supplies with the upload (which column is
the date, which is the amount, ...). "No parser recognised this file" and
"use the mapping the user gave us" are two different decisions, so the
registry reports the first and the import service (Step 7) makes the
second.
"""

from collections.abc import Sequence

from app.parsers.base import FileType, ParserInput, StatementParser

# Below this confidence we treat a parser as "not mine". 0.5 means a parser
# has to be more sure than unsure. Parsers are expected to score high (0.9+)
# when they see their bank's header and 0.0 when they do not, so the exact
# threshold rarely decides anything - it is a safety net against a parser
# that returns a lukewarm score for a file it would then misread.
DETECTION_THRESHOLD = 0.5


class ParserRegistry:
    """Holds the known parsers and chooses between them.

    An instance, not module-level global state: Step 6 provides a function
    that builds a registry with every real parser in it, and each test can
    build its own small registry without one test's parsers leaking into
    another's.
    """

    def __init__(self) -> None:
        self._parsers: list[StatementParser] = []

    def register(self, parser: StatementParser) -> None:
        """Add a parser, rejecting one that is obviously misconfigured.

        These checks are cheap and run at startup, where a mistake is easy
        to see. The alternative is a parser that is silently never chosen
        because `file_types` is empty, which is a confusing bug to chase
        from "my upload says unsupported format".
        """
        if not parser.bank_code:
            raise ValueError("parser has no bank_code")
        if not parser.file_types:
            raise ValueError(f"parser {parser.bank_code} declares no file_types")

        for existing in self._parsers:
            overlap = set(existing.file_types) & set(parser.file_types)
            if existing.bank_code == parser.bank_code and overlap:
                # Two parsers for the same bank are fine (HDFC CSV and HDFC
                # PDF), but two for the same bank AND the same file type
                # would compete for every file of that type.
                raise ValueError(
                    f"parser {parser.bank_code} is already registered for "
                    f"{sorted(file_type.value for file_type in overlap)}"
                )

        self._parsers.append(parser)

    @property
    def parsers(self) -> tuple[StatementParser, ...]:
        """The registered parsers, in registration order (read-only copy)."""
        return tuple(self._parsers)

    def detect(
        self,
        data: ParserInput,
        *,
        threshold: float = DETECTION_THRESHOLD,
    ) -> StatementParser | None:
        """Return the most confident parser for this file, or None.

        Parsers that do not handle the file's type are skipped before
        `detect()` is called, so a PDF parser never has to defend itself
        against a CSV.

        On a tie the earliest registered parser wins. That is what
        `max()` does with equal keys, and it makes the outcome
        deterministic (and therefore testable) instead of depending on
        dictionary or set ordering.

        A parser whose `detect()` raises is a bug in that parser, and we let
        the exception through rather than hiding it: the import fails
        loudly with the generic 500 and we see the traceback, instead of
        the file quietly being treated as "unrecognised" for weeks.
        """
        # Score every candidate once and keep the pairs: calling detect()
        # again for the threshold check would do the same work twice.
        scored = [(parser.detect(data), parser) for parser in self._candidates(data.file_type)]
        if not scored:
            return None

        # key=... compares only the score, so two parsers never have to be
        # compared with each other (they are not orderable), and max()
        # returns the first of equal scores.
        best_score, best_parser = max(scored, key=lambda pair: pair[0])
        return best_parser if best_score >= threshold else None

    def _candidates(self, file_type: FileType) -> Sequence[StatementParser]:
        return [parser for parser in self._parsers if file_type in parser.file_types]


def build_default_registry() -> ParserRegistry:
    """Build a registry holding every real bank parser (promised in Step 5).

    One function instead of a module-level `REGISTRY = ...`: a shared global
    would be mutable state that any test could add to and leak into the
    next, and FastAPI can hand this to a route as a cached dependency when
    Step 7 needs it.

    `GenericCsvParser` is deliberately NOT here. It needs the user's column
    mapping, so it cannot be built without a request - see the note at the
    top of this module.

    The import sits inside the function on purpose. This module is the
    mechanism and the parsers are the catalogue; importing them at the top
    would mean that anything touching `ParserRegistry` - including a test
    for the registry itself - also loads xlrd today and pdfplumber
    tomorrow.
    """
    from app.parsers.icici import IciciXlsParser

    registry = ParserRegistry()
    registry.register(IciciXlsParser())
    return registry
