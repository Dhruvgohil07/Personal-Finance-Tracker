"""ICICI PDF statements end to end (Phase 2 Step 9, ADR 019).

Synthetic PDFs from tests/fixtures/icici_pdf.py go through POST /uploads
and the synchronous test worker, exactly like the `.xls` tests. The key
test is the plan's: the SAME transactions imported as `.xls` and as PDF
are the same rows - the second import is 100% duplicates - which holds only
if the PDF narration is rebuilt exactly as the `.xls` prints it (the name
line dropped, wrapped lines joined with the spaces the breaks hid).

All names, VPAs and numbers are invented (CLAUDE.md rule 8).
"""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.services import imports as imports_service
from tests.fixtures.icici_pdf import build_icici_pdf
from tests.integration.test_categorization import ROWS as UPI_ROWS
from tests.integration.test_uploads import (
    AuthHeadersFactory,
    import_file,
    make_account,
    many_rows,
    statement_bytes,
    transactions_of,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def alice(make_auth_headers: AuthHeadersFactory) -> dict[str, str]:
    return make_auth_headers("alice@example.com")


def pdf_bytes(tmp_path: Path, rows: tuple[tuple[str, ...], ...], **kwargs: Any) -> bytes:
    return build_icici_pdf(tmp_path / "statement.pdf", rows, **kwargs).read_bytes()


def import_pdf(
    client: TestClient,
    headers: dict[str, str],
    account_id: str,
    content: bytes,
) -> dict[str, Any]:
    return import_file(
        client, headers, content=content, account_id=account_id, filename="statement.pdf"
    )


def test_a_pdf_statement_is_imported(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    account_id = make_account(client, alice)

    body = import_pdf(client, alice, account_id, pdf_bytes(tmp_path, UPI_ROWS))

    assert body["status"] == "completed"
    assert (body["rows_parsed"], body["rows_inserted"]) == (len(UPI_ROWS), len(UPI_ROWS))
    assert body["reconciliation_errors"] == 0
    assert (body["period_start"], body["period_end"]) == ("2026-08-01", "2026-08-31")
    rows = transactions_of(db, body["id"])
    assert [row.raw_description for row in rows] == [row[4] for row in UPI_ROWS]


@pytest.mark.parametrize("pdf_first", [False, True], ids=["xls_then_pdf", "pdf_then_xls"])
def test_the_same_statement_as_xls_and_pdf_is_100_percent_duplicates(
    client: TestClient, alice: dict[str, str], tmp_path: Path, pdf_first: bool
) -> None:
    """The plan's key test: one set of transactions, two file formats."""
    account_id = make_account(client, alice)
    xls = statement_bytes(tmp_path, rows=UPI_ROWS)
    pdf = pdf_bytes(tmp_path, UPI_ROWS)
    first, second = (pdf, xls) if pdf_first else (xls, pdf)
    names = ("statement.pdf", "statement.xls") if pdf_first else ("statement.xls", "statement.pdf")

    import_file(client, alice, content=first, account_id=account_id, filename=names[0])
    body = import_file(client, alice, content=second, account_id=account_id, filename=names[1])

    assert body["status"] == "completed"
    assert (body["rows_parsed"], body["rows_inserted"], body["rows_duplicate"]) == (
        len(UPI_ROWS),
        0,
        len(UPI_ROWS),
    )


def test_a_multi_page_pdf_keeps_every_row_in_order(
    client: TestClient, alice: dict[str, str], tmp_path: Path, db: Session
) -> None:
    rows = many_rows(120)  # several pages of rows
    account_id = make_account(client, alice)

    body = import_pdf(client, alice, account_id, pdf_bytes(tmp_path, rows))

    assert body["status"] == "completed"
    assert body["rows_inserted"] == 120
    assert body["reconciliation_errors"] == 0  # every balance chains across pages
    stored = sorted(transactions_of(db, body["id"]), key=lambda row: row.statement_seq)
    assert [row.raw_description for row in stored] == [row[4] for row in rows]


def test_a_pdf_for_another_account_fails_with_a_message(
    client: TestClient, alice: dict[str, str], tmp_path: Path
) -> None:
    account_id = make_account(client, alice, masked_number="9999")

    body = import_pdf(client, alice, account_id, pdf_bytes(tmp_path, UPI_ROWS))

    assert body["status"] == "failed"
    assert body["error_message"] == imports_service.WRONG_ACCOUNT_NUMBER_MESSAGE


def test_a_password_protected_pdf_fails_with_a_message_for_now(
    client: TestClient, alice: dict[str, str], tmp_path: Path
) -> None:
    """Step 10 adds passwords; until then the user is told what is wrong."""
    account_id = make_account(client, alice)

    body = import_pdf(client, alice, account_id, pdf_bytes(tmp_path, UPI_ROWS, password="1234"))

    assert body["status"] == "failed"
    assert "password-protected" in body["error_message"]
