"""Tests for the Redis-free parts of app/core/rate_limit.py.

Three groups:

- the KEY functions, which decide who a request is counted against;
- the signed client IP from the Next.js proxy (ADR 007, Phase 3 Step 2);
- `_retry_after_seconds`, which reads how long a client must wait.

Both are functions of the request, so no Redis is needed here; the limits
themselves are tested against real Redis in
tests/integration/test_rate_limit.py.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from limits import RateLimitItemPerMinute
from pydantic import SecretStr
from redis.exceptions import RedisError
from starlette.requests import Request

from app.core import rate_limit
from app.core.config import get_settings
from app.core.rate_limit import (
    CLIENT_IP_HEADER,
    CLIENT_IP_SIGNATURE_HEADER,
    _retry_after_seconds,
    client_host,
    client_ip,
    limiter,
    sign_client_ip,
    user_or_ip_key,
)
from app.core.security import create_access_token

USER_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def make_request(
    *,
    authorization: str | None = None,
    forwarded_for: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Request:
    """A bare Starlette Request from client 203.0.113.7 (a documentation IP).

    A "scope" is the dict an ASGI server builds for each request; headers
    are (name, value) byte pairs with lowercase names. latin-1 is how HTTP
    carries header bytes (and what Starlette decodes them with).
    """
    headers = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode()))
    for name, value in (extra_headers or {}).items():
        headers.append((name.lower().encode(), value.encode("latin-1")))
    scope = {"type": "http", "headers": headers, "client": ("203.0.113.7", 50000)}
    return Request(scope)


def test_client_ip_uses_the_connection_address() -> None:
    assert client_ip(make_request()) == "ip:203.0.113.7"


def test_client_ip_ignores_x_forwarded_for() -> None:
    # Otherwise an attacker could pick a new fake IP for every request.
    request = make_request(forwarded_for="198.51.100.1")
    assert client_ip(request) == "ip:203.0.113.7"


def test_client_ip_without_client_info() -> None:
    request = Request({"type": "http", "headers": [], "client": None})
    assert client_ip(request) == "ip:unknown"


def test_valid_access_token_is_counted_per_user() -> None:
    token = create_access_token(USER_ID)
    request = make_request(authorization=f"Bearer {token}")
    assert user_or_ip_key(request) == f"user:{USER_ID}"


def test_bearer_scheme_is_case_insensitive() -> None:
    token = create_access_token(USER_ID)
    request = make_request(authorization=f"bearer {token}")
    assert user_or_ip_key(request) == f"user:{USER_ID}"


def test_no_token_is_counted_per_ip() -> None:
    assert user_or_ip_key(make_request()) == "ip:203.0.113.7"


def test_garbage_token_is_counted_per_ip() -> None:
    # Sending an invalid token must not create a fresh counter.
    request = make_request(authorization="Bearer not-a-jwt")
    assert user_or_ip_key(request) == "ip:203.0.113.7"


def test_expired_token_is_counted_per_ip() -> None:
    long_ago = datetime.now(UTC) - timedelta(days=1)
    token = create_access_token(USER_ID, now=long_ago)
    request = make_request(authorization=f"Bearer {token}")
    assert user_or_ip_key(request) == "ip:203.0.113.7"


def test_other_auth_scheme_is_counted_per_ip() -> None:
    request = make_request(authorization="Basic dXNlcjpwYXNz")
    assert user_or_ip_key(request) == "ip:203.0.113.7"


# --- The signed client IP from the Next.js proxy ---------------------------
#
# The API sees every proxied request coming from the proxy's servers. The
# proxy therefore sends the browser's IP plus an HMAC of it, and the backend
# uses that IP only when the HMAC matches its own PROXY_SHARED_SECRET.

PROXY_SECRET = "kharcha-test-proxy-secret-0123456789abcdef"
BROWSER_IP = "198.51.100.23"  # documentation range (RFC 5737)
SOCKET_IP = "203.0.113.7"  # where make_request's connection comes from


@pytest.fixture
def proxy_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    """Configure PROXY_SHARED_SECRET for one test.

    get_settings() is cached, so changing the cached object changes it for
    the code under test too; monkeypatch puts the old value back.
    """
    monkeypatch.setattr(get_settings(), "proxy_shared_secret", SecretStr(PROXY_SECRET))
    return PROXY_SECRET


def proxied(ip: str, signature: str) -> Request:
    return make_request(extra_headers={CLIENT_IP_HEADER: ip, CLIENT_IP_SIGNATURE_HEADER: signature})


def test_the_signature_is_hmac_sha256_in_lowercase_hex() -> None:
    """A fixed test vector: the proxy's JavaScript must produce exactly this.

    Checked independently with OpenSSL:
    printf '198.51.100.23' | openssl dgst -sha256 -hmac '<PROXY_SECRET>'
    """
    assert sign_client_ip(BROWSER_IP, PROXY_SECRET) == (
        "9d72ea7f55c57296072946f85fbc5b8c831ab42c447ef86ce6c4fc4c08e97ce8"
    )


def test_a_correctly_signed_ip_is_used(proxy_secret: str) -> None:
    request = proxied(BROWSER_IP, sign_client_ip(BROWSER_IP, proxy_secret))

    assert client_host(request) == BROWSER_IP
    assert client_ip(request) == f"ip:{BROWSER_IP}"
    # Logged-out requests (the general limit) are counted by it too.
    assert user_or_ip_key(request) == f"ip:{BROWSER_IP}"


def test_without_a_configured_secret_the_header_is_ignored() -> None:
    """Local development: no proxy, so nobody's header is trusted."""
    assert get_settings().proxy_shared_secret is None
    request = proxied(BROWSER_IP, sign_client_ip(BROWSER_IP, PROXY_SECRET))

    assert client_host(request) == SOCKET_IP


@pytest.mark.parametrize(
    "headers",
    [
        {CLIENT_IP_HEADER: BROWSER_IP},  # no signature
        {CLIENT_IP_SIGNATURE_HEADER: sign_client_ip(BROWSER_IP, PROXY_SECRET)},  # no IP
        {},  # not proxied at all
    ],
    ids=["no_signature", "no_ip", "neither"],
)
def test_a_missing_header_falls_back_to_the_connection(
    proxy_secret: str, headers: dict[str, str]
) -> None:
    assert client_host(make_request(extra_headers=headers)) == SOCKET_IP


@pytest.mark.parametrize(
    "signature",
    [
        sign_client_ip(BROWSER_IP, "another-secret-of-at-least-32-characters"),
        # A valid signature, but of a DIFFERENT IP: it can't be moved.
        sign_client_ip("192.0.2.1", PROXY_SECRET),
        sign_client_ip(BROWSER_IP, PROXY_SECRET).upper(),  # the spelling is exact
        "",
        # Non-ASCII: hmac.compare_digest raises TypeError on such a str,
        # which would be a 500 on every request (compared as bytes instead).
        "\u00e9" * 64,
    ],
    ids=["other_secret", "other_ip", "uppercase", "empty", "non_ascii"],
)
def test_a_wrong_signature_falls_back_to_the_connection(proxy_secret: str, signature: str) -> None:
    """A forged header gets the attacker nothing: they are counted as themselves."""
    assert client_host(proxied(BROWSER_IP, signature)) == SOCKET_IP


def test_a_signed_value_that_is_not_an_ip_is_ignored(proxy_secret: str) -> None:
    """Only a bug in the proxy could sign this; it must not become a key."""
    value = "not-an-ip"
    request = proxied(value, sign_client_ip(value, proxy_secret))

    assert client_host(request) == SOCKET_IP


def test_an_ipv6_address_is_counted_under_one_spelling(proxy_secret: str) -> None:
    """2001:DB8::1 and 2001:0db8:0000::0001 are one address, so one counter."""
    for spelling in ("2001:DB8::1", "2001:0db8:0000::0001"):
        request = proxied(spelling, sign_client_ip(spelling, proxy_secret))
        assert client_host(request) == "2001:db8::1"


# --- Retry-After ----------------------------------------------------------
#
# When a limit is hit, slowapi leaves the limit it hit on the request as
# `state.view_rate_limit = (limit_item, key_parts)`, and
# `_retry_after_seconds` asks the storage when that window ends. The header
# is advisory, so every failure answers None ("no Retry-After header")
# instead of breaking the 429 the client is waiting for.

LIMIT_ITEM = RateLimitItemPerMinute(5)
KEY_PARTS = ("ip:203.0.113.7",)


def _request_that_hit_a_limit() -> Request:
    request = make_request()
    request.state.view_rate_limit = (LIMIT_ITEM, KEY_PARTS)
    return request


def test_retry_after_is_unknown_without_a_recorded_limit() -> None:
    # The 429 handler also runs for a RateLimitExceeded raised anywhere
    # else, where nothing recorded WHICH window was full.
    assert _retry_after_seconds(make_request()) is None


def test_retry_after_is_unknown_when_redis_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fail open on the header too: a Redis outage must not turn a 429 into
    # a 500 (ADR 007).
    def boom(*args: object, **kwargs: object) -> None:
        raise RedisError("connection refused")

    monkeypatch.setattr(limiter.limiter, "get_window_stats", boom)
    assert _retry_after_seconds(_request_that_hit_a_limit()) is None


@pytest.mark.parametrize(
    ("seconds_left", "expected"),
    [
        (0.2, 1),  # never "Retry-After: 0", which reads as "try again now"
        (4.2, 5),  # always rounded UP, so the retry can't be too early
        (60.0, 60),
    ],
)
def test_retry_after_rounds_up_to_whole_seconds(
    monkeypatch: pytest.MonkeyPatch, seconds_left: float, expected: int
) -> None:
    # `rate_limit.time` IS the standard time module, so this freezes the
    # clock the function reads (monkeypatch puts it back afterwards).
    # Without it the few microseconds between the two lines below would
    # decide the 4.2 -> 5 case, which is exactly the rounding being tested.
    now = 1_000_000.0
    monkeypatch.setattr(rate_limit.time, "time", lambda: now)
    monkeypatch.setattr(limiter.limiter, "get_window_stats", lambda *a: (now + seconds_left, 0))

    assert _retry_after_seconds(_request_that_hit_a_limit()) == expected
