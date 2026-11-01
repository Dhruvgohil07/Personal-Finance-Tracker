"""Re-running every user's rows after a chain change (review of Step 11).

`python -m app.workers.recategorize_all` exists because rows are
categorized when imported: a change to the chain itself (here, the Google
Pay for Business handle the Phase 2 acceptance run found) would otherwise
reach only statements imported afterwards. The key test stages exactly
that: a row stored as a "person" by the old chain, brought up to date by
the command. All data is synthetic (CLAUDE.md rule 8).
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rq import Queue
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.categorizer.base import CategorySource
from app.db.seed import run_seed
from app.models import Category, Transaction, User
from app.services import categorization as categorization_service
from app.workers import recategorize_all
from app.workers.queue import RECATEGORIZE_USER
from tests.fixtures.icici_xls import build_icici_xls
from tests.integration.test_categorization import upi
from tests.integration.test_uploads import AuthHeadersFactory, import_file, make_account

pytestmark = pytest.mark.integration

# One payment to a shop on Google Pay for Business, printed cut to 14
# characters the way the real October PDF has it.
BUSINESS_ROWS = (
    ("1", "01/08/2026", "01/08/2026", "",
     upi("GP SHOP", "ab1234@okbizax", "NO REMARK", "AXIS BANK", "512345678901"),
     "80.00", "0.00", "9920.00"),
)  # fmt: skip


@pytest.fixture
def seeded(db: Session) -> None:
    run_seed(db)


def test_every_user_gets_one_job_with_only_their_id(
    client: TestClient,
    make_auth_headers: AuthHeadersFactory,
    db: Session,
    held_queue: Queue,
) -> None:
    """One `recategorize_user` job per user, and its only argument is the
    user id (job arguments sit in Redis unencrypted, ADR 017)."""
    make_auth_headers("alice@example.com")
    make_auth_headers("bob@example.com")
    user_ids = {str(user_id) for user_id in db.scalars(select(User.id))}

    queued = categorization_service.recategorize_everyone(db, held_queue)

    jobs = held_queue.get_jobs()
    assert queued == len(jobs) == 2
    assert {job.func_name for job in jobs} == {RECATEGORIZE_USER}
    assert {job.args for job in jobs} == {(user_id,) for user_id in user_ids}


def test_no_users_queues_nothing(db: Session, held_queue: Queue) -> None:
    assert categorization_service.recategorize_everyone(db, held_queue) == 0
    assert held_queue.get_jobs() == []


@pytest.mark.usefixtures("seeded")
def test_the_command_brings_rows_stored_by_an_older_chain_up_to_date(
    client: TestClient,
    make_auth_headers: AuthHeadersFactory,
    db: Session,
    migrated_engine: Engine,
    sync_queue: Queue,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`main()`, the way a deploy runs it. The row is first set back to
    what the chain decided BEFORE the business handle was known - a
    transfer to a person - and the command must re-decide it."""
    alice = make_auth_headers("alice@example.com")
    account_id = make_account(client, alice)
    statement = build_icici_xls(tmp_path / "s.xls", BUSINESS_ROWS).read_bytes()
    upload = import_file(client, alice, content=statement, account_id=account_id)
    people = db.scalar(select(Category.id).where(Category.slug == "transfers-to-people"))
    db.execute(
        update(Transaction).values(category_id=people, category_source=CategorySource.HEURISTIC)
    )
    db.commit()
    # Point the command at the test database and the synchronous queue,
    # never at the development ones (see test_seed.py).
    monkeypatch.setattr(recategorize_all, "SessionLocal", sessionmaker(bind=migrated_engine))
    monkeypatch.setattr(recategorize_all, "get_queue", lambda: sync_queue)

    recategorize_all.main()

    db.expire_all()
    [row] = db.scalars(select(Transaction).where(Transaction.upload_id == upload["id"]))
    assert (row.category_id, row.category_source) == (None, CategorySource.NONE)
