"""GET /uploads, GET /uploads/{id} and DELETE /uploads/{id}.

Uploads are created through the real POST, on the synchronous test queue,
so each one has already been imported (or has failed) when it is listed.

The rules defended here:

- every route answers 404 for another user's upload, exactly like for an
  unknown id (CLAUDE.md rule 2, IDOR)
- DELETE removes the upload, the transactions it inserted and its stored
  file - and nothing that belongs to another upload
- DELETE is refused (409) while the worker is processing the upload
"""

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import StatementUpload, Transaction, UploadBlob, UploadStatus
from app.services import uploads as uploads_service
from tests.fixtures.icici_xls import build_icici_xls

pytestmark = pytest.mark.integration

UPLOADS = "/api/v1/uploads"
ACCOUNTS = "/api/v1/accounts"

AuthHeadersFactory = Callable[[str], dict[str, str]]

# A statement for 3-5 August that overlaps the default fixture (1-4
# August) in two rows; only the 5 August row is new. Same rows as in
# test_uploads.py.
OVERLAPPING_ROWS = (
    ("1", "03/08/2026", "03/08/2026", "", "NEFT-SALARY-ACME", "0.00", "50000.00", "161431.33"),
    ("2", "04/08/2026", "04/08/2026", "", "MMT/IMPS/ACME BANK LTD", "1500.00", "0.00", "159931.33"),
    ("", "", "", "", "/205412345678/PAYMENT", "", "", ""),
    ("3", "05/08/2026", "05/08/2026", "", "UPI/NEWSHOP/PAY", "300.00", "0.00", "159631.33"),
)


@pytest.fixture
def alice(make_auth_headers: AuthHeadersFactory) -> dict[str, str]:
    return make_auth_headers("alice@example.com")


@pytest.fixture
def bob(make_auth_headers: AuthHeadersFactory) -> dict[str, str]:
    return make_auth_headers("bob@example.com")


def make_account(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post(
        ACCOUNTS,
        json={
            "bank_code": "ICICI",
            "nickname": "Salary account",
            "account_type": "savings",
            "masked_number": "1234",
        },
        headers=headers,
    )
    assert response.status_code == 201
    account_id: str = response.json()["id"]
    return account_id


@pytest.fixture
def account_id(client: TestClient, alice: dict[str, str]) -> str:
    return make_account(client, alice)


def upload(
    client: TestClient,
    headers: dict[str, str],
    account_id: str,
    tmp_path: Path,
    *,
    name: str = "statement.xls",
    **statement: Any,
) -> str:
    """Upload a synthetic ICICI statement; return the upload id."""
    content = build_icici_xls(tmp_path / name, **statement).read_bytes()
    response = client.post(
        UPLOADS,
        headers=headers,
        files={"file": (name, content, "application/octet-stream")},
        data={"account_id": account_id},
    )
    assert response.status_code == 202
    upload_id: str = response.json()["id"]
    return upload_id


def count(db: Session, model: type[Any]) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


# --- Authentication ---------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", UPLOADS),
        ("GET", f"{UPLOADS}/{uuid.uuid4()}"),
        ("DELETE", f"{UPLOADS}/{uuid.uuid4()}"),
    ],
)
def test_every_route_needs_a_token(client: TestClient, method: str, path: str) -> None:
    assert client.request(method, path).status_code == 401


# --- GET /uploads -----------------------------------------------------------


def test_a_new_user_has_no_uploads(client: TestClient, alice: dict[str, str]) -> None:
    response = client.get(UPLOADS, headers=alice)

    assert response.status_code == 200
    assert response.json() == []


def test_uploads_are_listed_newest_first_failed_ones_included(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path
) -> None:
    """The list is the user's history of attempts, so a failed one is in it
    with its reason - that is how the user finds out what went wrong."""
    first = upload(client, alice, account_id, tmp_path)
    broken = upload(
        client,
        alice,
        account_id,
        tmp_path,
        name="broken.xls",
        rows=(("1", "01/08/2026", "01/08/2026", "", "UPI/SHOP/PAY", "x", "", "1.00"),),
    )

    body = client.get(UPLOADS, headers=alice).json()

    assert [item["id"] for item in body] == [broken, first]
    assert [item["status"] for item in body] == ["failed", "completed"]
    assert body[0]["error_message"] is not None
    assert "file_sha256" not in body[0]
    assert "column_mapping" not in body[0]


def test_only_your_own_uploads_are_listed(
    client: TestClient,
    alice: dict[str, str],
    bob: dict[str, str],
    account_id: str,
    tmp_path: Path,
) -> None:
    upload(client, alice, account_id, tmp_path)

    assert client.get(UPLOADS, headers=bob).json() == []


# --- GET /uploads/{id} ------------------------------------------------------


def test_one_upload_can_be_read(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path
) -> None:
    upload_id = upload(client, alice, account_id, tmp_path)

    response = client.get(f"{UPLOADS}/{upload_id}", headers=alice)

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == upload_id
    assert body["status"] == "completed"
    assert body["rows_inserted"] == 5


def test_another_users_upload_is_not_found(
    client: TestClient,
    alice: dict[str, str],
    bob: dict[str, str],
    account_id: str,
    tmp_path: Path,
) -> None:
    """IDOR: the same 404 as an id that does not exist, so Bob cannot even
    tell that the id is real."""
    upload_id = upload(client, alice, account_id, tmp_path)

    response = client.get(f"{UPLOADS}/{upload_id}", headers=bob)
    unknown = client.get(f"{UPLOADS}/{uuid.uuid4()}", headers=bob)

    assert response.status_code == 404
    assert response.json() == unknown.json()
    assert response.json()["error"]["message"] == uploads_service.UPLOAD_NOT_FOUND_MESSAGE


def test_an_invalid_upload_id_is_a_validation_error(
    client: TestClient, alice: dict[str, str]
) -> None:
    assert client.get(f"{UPLOADS}/not-a-uuid", headers=alice).status_code == 422


# --- DELETE /uploads/{id} ---------------------------------------------------


def test_deleting_an_upload_removes_its_transactions(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    upload_id = upload(client, alice, account_id, tmp_path)
    assert count(db, Transaction) == 5

    response = client.delete(f"{UPLOADS}/{upload_id}", headers=alice)

    assert response.status_code == 204
    assert count(db, StatementUpload) == 0
    assert count(db, Transaction) == 0
    assert client.get(f"{UPLOADS}/{upload_id}", headers=alice).status_code == 404


def test_deleting_an_upload_keeps_the_rows_another_upload_inserted(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    """Undo removes exactly what this upload ADDED.

    The overlapping statement shares two rows with the first one. Those
    rows were inserted by the first upload and only skipped as duplicates
    by the second, so deleting the second removes just its one new row.
    """
    first = upload(client, alice, account_id, tmp_path)
    second = upload(
        client,
        alice,
        account_id,
        tmp_path,
        name="second.xls",
        rows=OVERLAPPING_ROWS,
        period_from="03/08/2026",
        period_to="05/08/2026",
    )
    assert count(db, Transaction) == 6

    assert client.delete(f"{UPLOADS}/{second}", headers=alice).status_code == 204

    remaining = db.scalars(select(Transaction.upload_id)).all()
    assert len(remaining) == 5
    assert set(remaining) == {uuid.UUID(first)}


def test_a_deleted_upload_can_be_uploaded_again(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path
) -> None:
    """With the row gone, the file is no longer "already uploaded"."""
    upload_id = upload(client, alice, account_id, tmp_path)
    client.delete(f"{UPLOADS}/{upload_id}", headers=alice)

    again = upload(client, alice, account_id, tmp_path)

    assert client.get(f"{UPLOADS}/{again}", headers=alice).json()["rows_inserted"] == 5


def test_another_users_upload_cannot_be_deleted(
    client: TestClient,
    alice: dict[str, str],
    bob: dict[str, str],
    account_id: str,
    tmp_path: Path,
    db: Session,
) -> None:
    upload_id = upload(client, alice, account_id, tmp_path)

    response = client.delete(f"{UPLOADS}/{upload_id}", headers=bob)

    assert response.status_code == 404
    assert count(db, StatementUpload) == 1
    assert count(db, Transaction) == 5


def test_an_unknown_upload_cannot_be_deleted(client: TestClient, alice: dict[str, str]) -> None:
    assert client.delete(f"{UPLOADS}/{uuid.uuid4()}", headers=alice).status_code == 404


def test_an_upload_being_processed_cannot_be_deleted(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    """409 while the worker runs: deleting the row under it would turn its
    import into a confusing failure halfway through. The row is set to
    `processing` by hand, the state a running import leaves it in."""
    upload_id = upload(client, alice, account_id, tmp_path)
    row = db.get_one(StatementUpload, uuid.UUID(upload_id))
    row.status = UploadStatus.PROCESSING
    db.commit()

    response = client.delete(f"{UPLOADS}/{upload_id}", headers=alice)

    assert response.status_code == 409
    assert response.json()["error"]["message"] == uploads_service.UPLOAD_PROCESSING_MESSAGE
    db.expire_all()
    assert count(db, StatementUpload) == 1


def test_a_pending_upload_can_be_deleted_with_its_file(
    client: TestClient, alice: dict[str, str], account_id: str, tmp_path: Path, db: Session
) -> None:
    """Still waiting in the queue: deleting it is allowed, and takes the
    stored file with it (ON DELETE CASCADE). The job will find nothing."""
    upload_id = upload(client, alice, account_id, tmp_path)
    row = db.get_one(StatementUpload, uuid.UUID(upload_id))
    row.status = UploadStatus.PENDING
    db.add(UploadBlob(upload_id=row.id, encrypted_content=b"x" * 32, nonce=b"n" * 12))
    db.commit()

    assert client.delete(f"{UPLOADS}/{upload_id}", headers=alice).status_code == 204

    assert count(db, StatementUpload) == 0
    assert count(db, UploadBlob) == 0
