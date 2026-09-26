"""Tests for app.core.logging: sensitive fields never reach the log output."""

import json
from collections.abc import Iterator

import pytest
import structlog

from app.core.logging import REDACTED, configure_logging, redact_sensitive_fields


def redact(event_dict: dict) -> dict:
    # The processor's first two arguments (logger, method name) are unused.
    return redact_sensitive_fields(None, "info", event_dict)


@pytest.fixture
def reset_structlog() -> Iterator[None]:
    """Undo configure_logging() after the test, so other tests start clean."""
    yield
    structlog.reset_defaults()


@pytest.mark.parametrize(
    "key",
    [
        "amount",
        "amount_paise",
        "Balance",
        "narration",
        "description",
        "password",
        "statement_password",
        "access_token",
        "refresh_token",
        "Authorization",
        "cookie",
        "X-Api-Key",
        "account_number",
        "accountNumber",
        "email",
        "file_content",
    ],
)
def test_sensitive_key_is_redacted(key: str) -> None:
    assert redact({key: "secret value"}) == {key: REDACTED}


@pytest.mark.parametrize("key", ["event", "user_id", "upload_id", "count", "status", "duration_ms"])
def test_safe_key_is_kept(key: str) -> None:
    assert redact({key: 42}) == {key: 42}


def test_nested_dicts_and_lists_are_redacted() -> None:
    event = {
        "user_id": 1,
        "rows": [{"row": 1, "amount": 1250}, {"row": 2, "narration": "UPI/RAHUL"}],
    }

    assert redact(event) == {
        "user_id": 1,
        "rows": [{"row": 1, "amount": REDACTED}, {"row": 2, "narration": REDACTED}],
    }


def test_redaction_does_not_modify_the_callers_dict() -> None:
    event = {"password": "hunter2hunter2"}

    redact(event)

    assert event == {"password": "hunter2hunter2"}


def test_log_output_is_json_with_sensitive_values_removed(
    reset_structlog: None, capsys: pytest.CaptureFixture[str]
) -> None:
    # End to end: the real configuration, a real log call, the real output.
    configure_logging("test")

    structlog.get_logger().info(
        "transaction_parsed", upload_id=7, amount=125000, narration="UPI/RAHUL SHARMA/9876@ybl"
    )

    output = capsys.readouterr().out
    line = json.loads(output)
    assert line["event"] == "transaction_parsed"
    assert line["level"] == "info"
    assert line["upload_id"] == 7
    assert line["amount"] == REDACTED
    assert "RAHUL" not in output
    assert "125000" not in output


def test_debug_logs_are_dropped_outside_development(
    reset_structlog: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("production")

    structlog.get_logger().debug("noisy_detail", upload_id=7)

    assert capsys.readouterr().out == ""


def test_nested_job_password_is_redacted() -> None:
    event = {"job_id": "abc", "job": {"kwargs": {"upload_id": 7, "statement_password": "secret"}}}
    assert redact(event) == {
        "job_id": "abc",
        "job": {"kwargs": {"upload_id": 7, "statement_password": REDACTED}},
    }
