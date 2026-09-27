"""Structured JSON logging, with automatic redaction of sensitive fields.

We log with structlog. Instead of formatting a sentence, you log an event name
plus key/value pairs:

    import structlog
    log = structlog.get_logger()
    log.info("upload_processed", upload_id=7, rows=120)

    -> {"upload_id": 7, "rows": 120, "event": "upload_processed",
        "level": "info", "timestamp": "2026-09-26T10:15:00Z"}

Every log call becomes a dict (the "event dict") that is passed through a
chain of *processors*: small functions that each take the dict and return a
(possibly changed) dict. The last processor turns it into a JSON line.

One of those processors, `redact_sensitive_fields`, replaces the value of
any sensitive key with "[REDACTED]". This enforces the "never log sensitive
data" rule automatically, so it doesn't depend on every developer
remembering it.

Limitation: redaction only sees *keys*. Text you put into the event name
itself (e.g. log.info(f"parsed {narration}")) cannot be caught, so the rule
"log IDs and counts only" still applies. Redaction is a safety net.
"""

import logging
import sys
from typing import Any

import structlog
from structlog.typing import EventDict, WrappedLogger

REDACTED = "[REDACTED]"

# A key is sensitive if it CONTAINS any of these parts (after normalizing,
# see `_normalize_key`). Substring matching means "token" also catches
# "access_token" and "refresh_token", and "amount" catches "amount_paise".
# It may over-redact a harmless key like "token_count"; hiding too much is
# the safe way to fail, hiding too little is a data leak.
SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    # Money (amounts are stored as paise, e.g. amount_paise)
    "amount",
    "balance",
    # Transaction text: narrations contain names, UPI IDs, phone numbers
    "narration",
    "description",
    # Credentials: login/statement passwords, JWTs, API keys, the file key
    "password",
    "token",
    "secret",
    "authorization",
    "cookie",
    "apikey",
    # Personal identifiers: log user_id instead
    "accountnumber",
    "email",
    # Raw uploaded file data
    "filecontent",
)


def _normalize_key(key: object) -> str:
    """Lowercase and drop '_' and '-' so all spellings compare equal.

    "account_number", "accountNumber" and "Account-Number" all become
    "accountnumber"; "X-Api-Key" becomes "xapikey" (which contains "apikey").
    """
    return str(key).lower().replace("_", "").replace("-", "")


def is_sensitive_key(key: object) -> bool:
    normalized = _normalize_key(key)
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def _redact(value: Any) -> Any:
    """Return a copy of `value` with sensitive keys redacted, at any depth.

    Recursion handles nested data such as
    {"job": {"kwargs": {"password": "..."}}} or a list of dicts.
    We build new dicts/lists instead of editing in place, so the caller's
    own objects are never modified by logging.
    """
    if isinstance(value, dict):
        return {
            key: REDACTED if is_sensitive_key(key) else _redact(item) for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact(item) for item in value]
    return value


def redact_sensitive_fields(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """structlog processor: redact sensitive keys in the event dict.

    Every structlog processor has this signature (logger, method name such as
    "info", event dict) and returns the event dict. We only need the dict.
    """
    return _redact(event_dict)


def configure_logging(env: str) -> None:
    """Set up structlog once, at app startup (called from main.py in Step 4).

    `env` comes from Settings.env: development logs DEBUG and above, test and
    production log INFO and above.
    """
    level = logging.DEBUG if env == "development" else logging.INFO

    structlog.configure(
        processors=[
            # Adds fields bound for the current request (e.g. request_id,
            # added in a later step) to every log line.
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            # Turns exc_info=True into a "exception" text field.
            structlog.processors.format_exc_info,
            # Runs after everything that ADDS fields, and right before the
            # renderer, so it sees every field that will be written.
            redact_sensitive_fields,
            structlog.processors.JSONRenderer(),
        ],
        # Drops calls below `level` cheaply (log.debug in production is a no-op).
        wrapper_class=structlog.make_filtering_bound_logger(level),
        # Write each JSON line to stdout; Docker and hosting platforms collect stdout.
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
    )

    # uvicorn's access log ("GET /path?query HTTP/1.1" 200) uses Python's
    # standard `logging`, not structlog, so our redaction never sees it, and
    # it includes the query string (e.g. ?min_amount=5000). Turn it off;
    # RequestLoggingMiddleware (app/core/middleware.py) logs requests instead.
    # This runs when the app is imported, which uvicorn does AFTER setting up
    # its own logging, so this setting wins.
    logging.getLogger("uvicorn.access").disabled = True
