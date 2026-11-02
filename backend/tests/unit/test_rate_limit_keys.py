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
from structlog.testing import capture_logs

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
    client_address: str = "203.0.113.7",
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
    scope = {
        "type": "http",
        "path": "/api/v1/auth/login",
        "headers": headers,
        "client": (client_address, 50000),
    }
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


def test_without_a_configured_secret_the_header_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local development: no proxy, so nobody's header is trusted.

    The secret is removed here rather than assumed absent: a developer's
    .env may set it to run the Next.js proxy locally (review of PR 29).
    """
    monkeypatch.setattr(get_settings(), "proxy_shared_secret", None)
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


def test_an_ipv4_address_in_ipv6_form_is_the_ipv4_address(proxy_secret: str) -> None:
    """::ffff:198.51.100.23 IS 198.51.100.23: one spelling, one counter."""
    mapped = f"::ffff:{BROWSER_IP}"
    request = proxied(mapped, sign_client_ip(mapped, proxy_secret))

    assert client_host(request) == BROWSER_IP


# --- A bad signature is logged once, without values ------------------------


def test_a_bad_signature_is_logged_once_per_request(proxy_secret: str) -> None:
    """A wrong secret on one side must be noticeable, not silent.

    The key functions run several times per request; the answer is kept on
    the request, so the warning appears once. Neither the claimed IP nor the
    signature is in it.
    """
    signature = sign_client_ip(BROWSER_IP, "another-secret-of-at-least-32-characters")
    request = proxied(BROWSER_IP, signature)

    with capture_logs() as logs:
        client_host(request)
        client_ip(request)
        user_or_ip_key(request)

    assert logs == [
        {"event": "proxy_signature_invalid", "log_level": "warning", "path": "/api/v1/auth/login"}
    ]
    assert BROWSER_IP not in str(logs)
    assert signature not in str(logs)


def test_a_signed_value_that_is_not_an_ip_is_logged(proxy_secret: str) -> None:
    request = proxied("not-an-ip", sign_client_ip("not-an-ip", proxy_secret))

    with capture_logs() as logs:
        client_host(request)

    assert [log["event"] for log in logs] == ["proxy_signed_value_not_an_ip"]


def test_a_request_without_proxy_headers_logs_nothing(proxy_secret: str) -> None:
    """Calling the API directly is normal (e.g. /docs): no warning for it."""
    with capture_logs() as logs:
        client_host(make_request())

    assert logs == []


# --- IPv6: counted per /64 --------------------------------------------------
#
# An ISP gives a household or phone a whole /64 and devices choose new
# addresses inside it freely, so counting single IPv6 addresses would hand
# out a fresh login counter per request (review of PR 29).


def test_ipv6_addresses_in_one_slash_64_share_a_counter(proxy_secret: str) -> None:
    keys = set()
    for address in ("2001:db8:1:2::5", "2001:db8:1:2:abcd:ef01:2345:6789"):
        request = proxied(address, sign_client_ip(address, proxy_secret))
        keys.add(client_ip(request))

    assert keys == {"ip:2001:db8:1:2::/64"}


def test_ipv6_addresses_in_different_slash_64s_are_counted_apart(proxy_secret: str) -> None:
    first, second = "2001:db8:1:2::5", "2001:db8:1:3::5"
    keys = {client_ip(proxied(ip, sign_client_ip(ip, proxy_secret))) for ip in (first, second)}

    assert keys == {"ip:2001:db8:1:2::/64", "ip:2001:db8:1:3::/64"}


def test_the_audit_ip_is_still_the_full_ipv6_address(proxy_secret: str) -> None:
    """Only the COUNTING groups a /64; client_host keeps the exact address."""
    address = "2001:db8:1:2::5"
    request = proxied(address, sign_client_ip(address, proxy_secret))

    assert client_host(request) == address
    assert client_ip(request) == "ip:2001:db8:1:2::/64"


@pytest.mark.parametrize(
    ("connection", "expected_key"),
    [
        ("2001:db8:9:9::1", "ip:2001:db8:9:9::/64"),
        ("::1", "ip:::/64"),  # a small number, still read as IPv6
        ("fe80::1%eth0", "ip:fe80::/64"),  # a scope suffix is dropped
        ("::ffff:203.0.113.9", "ip:203.0.113.9"),  # not one /64 for every such client
    ],
)
def test_a_direct_ipv6_connection_is_counted_per_slash_64(
    connection: str, expected_key: str
) -> None:
    """The same rule applies without the proxy (the connection's address)."""
    assert client_ip(make_request(client_address=connection)) == expected_key


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
