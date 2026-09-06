import hashlib
import hmac
import json
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
import pytest

from cotrader.auth import session_token, telegram_user, verify_session
from cotrader.broker import BrokerError, TossBroker
from cotrader.config import Settings


def settings(**kwargs):
    return Settings(
        TOSS_INVEST_OPEN_API_CLIENT_ID="fixture-client",
        TOSS_INVEST_OPEN_API_CLIENT_SECRET="fixture-secret",
        **kwargs,
    )


def test_telegram_signature_user_expiry_and_duplicate_fields():
    token = "test-token"
    fields = {"auth_date": "1000", "user": json.dumps({"id": 7})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    payload = urlencode(fields)
    assert telegram_user(payload, token, 7, at=1100) == 7
    for data, user, at in [(payload, 8, 1100), (payload, 7, 1400), (payload + "&auth_date=1000", 7, 1100)]:
        with pytest.raises(ValueError):
            telegram_user(data, token, user, at=at)
    cookie = session_token(7, "fixture-signing-secret", at=1000)
    assert verify_session(cookie, "fixture-signing-secret", 7, at=1100) == 7
    with pytest.raises(ValueError):
        verify_session(cookie, "fixture-signing-secret", 7, at=5000)


async def test_token_is_reused_and_account_header_is_explicit():
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "fixture-access", "expires_in": 86400})
        return httpx.Response(
            200, json={"result": {"cashBuyingPower": "5000"}}, headers={"X-RateLimit-Limit": "6"}
        )

    broker = TossBroker(
        settings(account_seq=7),
        httpx.AsyncClient(base_url=TossBroker.base_url, transport=httpx.MockTransport(handle)),
    )
    assert await broker.buying_power() == 5000
    assert await broker.buying_power() == 5000
    assert sum(r.url.path == "/oauth2/token" for r in requests) == 1
    assert requests[-1].headers["X-Tossinvest-Account"] == "7"
    await broker.close()


async def test_mutating_timeout_is_ambiguous_and_never_automatically_retried():
    calls = []

    def handle(request):
        calls.append(request.url.path)
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "fixture-access", "expires_in": 86400})
        raise httpx.ReadTimeout("fixture timeout")

    broker = TossBroker(
        settings(
            account_seq=7,
            live_enabled=True,
            market_source="toss",
            auth_mode="telegram",
            runtime_role="engine",
        ),
        httpx.AsyncClient(base_url=TossBroker.base_url, transport=httpx.MockTransport(handle)),
    )
    with pytest.raises(BrokerError) as caught:
        await broker.request(
            "POST", "/api/v1/orders", group="ORDER", account=True, body={"clientOrderId": "stable"}
        )
    assert caught.value.ambiguous
    assert calls.count("/api/v1/orders") == 1
    await broker.close()


async def test_live_gate_prevents_any_order_request():
    calls = []
    broker = TossBroker(
        settings(),
        httpx.AsyncClient(
            base_url=TossBroker.base_url, transport=httpx.MockTransport(lambda r: calls.append(r))
        ),
    )
    with pytest.raises(BrokerError, match="live-disabled"):
        await broker.place(SimpleNamespace())
    assert calls == []
    await broker.close()


def test_live_mode_requires_secure_configuration():
    with pytest.raises(ValueError):
        Settings(live_enabled=True)


def test_configuration_errors_do_not_include_secret_inputs():
    with pytest.raises(ValueError) as exc:
        Settings(auth_mode="telegram", TELEGRAM_API_KEY="super-secret-value", TELEGRAM_ME=7)
    assert "super-secret-value" not in str(exc.value)


@pytest.mark.parametrize("code", ["invalid-token", "expired-token"])
async def test_read_refreshes_a_revoked_or_expired_cached_token(code):
    tokens, reads = 0, 0

    def handle(request):
        nonlocal tokens, reads
        if request.url.path == "/oauth2/token":
            tokens += 1
            return httpx.Response(200, json={"access_token": f"fixture-{tokens}", "expires_in": 86400})
        reads += 1
        if reads == 1:
            return httpx.Response(401, json={"error": {"code": code}})
        assert request.headers["Authorization"] == "Bearer fixture-2"
        return httpx.Response(200, json={"result": []})

    broker = TossBroker(
        settings(), httpx.AsyncClient(base_url=TossBroker.base_url, transport=httpx.MockTransport(handle))
    )
    try:
        assert await broker.accounts() == []
        assert tokens == 2 and reads == 2
    finally:
        await broker.close()


async def test_order_auth_failure_expires_token_without_resending_order():
    posts, tokens = 0, 0

    def handle(request):
        nonlocal posts, tokens
        if request.url.path == "/oauth2/token":
            tokens += 1
            return httpx.Response(200, json={"access_token": f"fixture-{tokens}", "expires_in": 86400})
        if request.method == "POST":
            posts += 1
            return httpx.Response(401, json={"error": {"code": "invalid-token"}})
        return httpx.Response(200, json={"result": []})

    broker = TossBroker(
        settings(runtime_role="engine", auth_mode="github", market_source="toss", live_enabled=True),
        httpx.AsyncClient(base_url=TossBroker.base_url, transport=httpx.MockTransport(handle)),
    )
    try:
        with pytest.raises(BrokerError, match="invalid-token"):
            await broker.request("POST", "/api/v1/orders", body={"clientOrderId": "fixture-stable"})
        assert posts == 1 and broker.expires == 0
        assert await broker.accounts() == []
        assert tokens == 2 and posts == 1
    finally:
        await broker.close()
