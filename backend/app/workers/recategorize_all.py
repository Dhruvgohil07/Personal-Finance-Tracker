"""Re-run categorization over every user's stored rows (review of Step 11).

    python -m app.workers.recategorize_all

Run once after deploying a change to the categorizer chain itself - a new
merchant shape, keyword or dictionary entry. Rows are categorized when
they are imported and re-run when their user changes a rule, so without
this a chain change would reach only statements imported afterwards: the
same shop could sit in two different categories in two different months.

It does not do the work itself. It queues one `recategorize_user` job per
user (`recategorize_everyone`) for the worker, so it needs Redis and a
running worker, and it returns in seconds however many rows there are.
Hand-chosen categories (`user_manual`) are never touched, as always.
"""

import structlog

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import SessionLocal
from app.services import categorization as categorization_service
from app.workers.queue import get_queue

log = structlog.get_logger()


def main() -> None:
    """Configure logging, then queue the re-run for every user."""
    configure_logging(get_settings().env)
    with SessionLocal() as db:
        queued = categorization_service.recategorize_everyone(db, get_queue())
    # A count only. `_count`, so the log redaction lets the integer through.
    log.info("recategorize_all_queued", user_count=queued)


if __name__ == "__main__":
    main()
