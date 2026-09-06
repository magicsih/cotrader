import logging
from datetime import timedelta
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from cotrader import github_auth
from cotrader.api import create_app
from cotrader.auth import session_token, verify_session
from cotrader.cli import PrivateQueryFilter
from cotrader.config import Settings
from cotrader.models import LoginChallenge, now


def config():
    return Settings(
        auth_mode="github",
        public_url="https://trading.example.com",
        github_user_id=42,
        github_client_id="fixture-client",
        github_client_secret="fixture-client-secret",
        session_secret="fixture-session-signing-secret-32-characters",
    )


def client_for(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("198.51.100.5", 5000)),
        base_url="https://trading.example.com",
        follow_redirects=False,
    )


async def test_login_pkce_state_single_use_and_owner_session(db, monkeypatch):
    _, sessions = db
    settings = config()
    app = create_app(settings, sessions_override=sessions)
    identity = AsyncMock(return_value=42)
    monkeypatch.setattr(github_auth, "github_identity", identity)
    async with client_for(app) as client:
        assert (await client.get("/api/account")).status_code == 401
        initial = await client.get("/api/auth/github")
        assert initial.status_code == 303
        query = parse_qs(urlsplit(initial.headers["location"]).query)
        assert query["redirect_uri"] == ["https://trading.example.com/api/auth/github/callback"]
        assert query["code_challenge_method"] == ["S256"] and "scope" not in query
        state = query["state"][0]
        assert all(flag in initial.headers["set-cookie"] for flag in ["Secure", "HttpOnly", "SameSite=lax"])
        async with sessions() as session:
            challenge = await session.get(LoginChallenge, github_auth.sha256(state))
            verifier = challenge.verifier
            assert query["code_challenge"] == [github_auth.pkce_challenge(verifier)]
        other_browser = {github_auth.STATE_COOKIE: "wrong"}
        async with client_for(app) as other:
            other.cookies.update(other_browser)
            failed = await other.get(
                "/api/auth/github/callback",
                params={"state": state, "code": "fixture-code"},
            )
            assert failed.status_code == 401
            identity.assert_not_called()
        success = await client.get(
            "/api/auth/github/callback", params={"state": state, "code": "fixture-code"}
        )
        assert success.headers["location"] == settings.public_url + "/"
        identity.assert_awaited_once_with(settings, "fixture-code", verifier)
        assert (await client.get("/api/auth/session")).json() == {"actor": "github:42"}
        assert (await client.get("/api/account")).status_code == 200
        client.cookies.set(github_auth.STATE_COOKIE, state, domain="trading.example.com", path="/")
        repeated = await client.get(
            "/api/auth/github/callback",
            params={"state": state, "code": "fixture-code"},
        )
        assert repeated.status_code == 401
        assert identity.await_count == 1
        assert (
            await client.post("/api/auth/logout", json={}, headers={"Origin": "https://evil.example"})
        ).status_code == 403
        assert (
            await client.post("/api/auth/logout", json={}, headers={"Origin": settings.public_url})
        ).status_code == 200
        assert (await client.get("/api/account")).status_code == 401


async def test_expired_and_denied_login_cannot_create_sessions(db, monkeypatch):
    _, sessions = db
    app = create_app(config(), sessions_override=sessions)
    identity = AsyncMock(side_effect=ValueError("fixture rejected"))
    monkeypatch.setattr(github_auth, "github_identity", identity)
    async with client_for(app) as client:
        response = await client.get("/api/auth/github")
        state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
        async with sessions.begin() as session:
            challenge = await session.get(LoginChallenge, github_auth.sha256(state))
            challenge.expires_at = now() - timedelta(seconds=1)
        assert (
            await client.get("/api/auth/github/callback", params={"state": state, "code": "fixture"})
        ).status_code == 401
        identity.assert_not_called()
        response = await client.get("/api/auth/github")
        state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
        denied = await client.get("/api/auth/github/callback", params={"state": state, "code": "fixture"})
        assert denied.status_code == 303 and "login_error" in denied.headers["location"]
        assert "cotrader_session=" not in denied.headers["set-cookie"]
        assert (await client.get("/api/strategies")).status_code == 401


@pytest.mark.parametrize("owner,scope,accepted", [(42, "", True), (43, "", False), (42, "repo", False)])
async def test_github_exchange_checks_actual_id_and_refuses_extra_scopes(owner, scope, accepted):
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.host == "github.com":
            form = parse_qs(request.content.decode())
            assert form["code_verifier"] == ["fixture-verifier"]
            assert form["redirect_uri"] == ["https://trading.example.com/api/auth/github/callback"]
            return httpx.Response(
                200, json={"access_token": "fixture-token", "token_type": "bearer", "scope": scope}
            )
        assert request.headers["authorization"] == "Bearer fixture-token"
        return httpx.Response(200, json={"id": owner, "login": "fixture-user"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        if accepted:
            assert (
                await github_auth.github_identity(config(), "fixture-code", "fixture-verifier", client) == 42
            )
        else:
            with pytest.raises(ValueError):
                await github_auth.github_identity(config(), "fixture-code", "fixture-verifier", client)
        if scope:
            assert len(calls) == 1


def test_sessions_are_bound_to_provider_and_current_owner():
    settings = config()
    cookie = session_token(42, settings.session_secret.get_secret_value(), provider="github")
    assert verify_session(cookie, settings.session_secret.get_secret_value(), 42, provider="github") == 42
    for provider, user in [("telegram", 42), ("github", 43)]:
        with pytest.raises(ValueError):
            verify_session(cookie, settings.session_secret.get_secret_value(), user, provider=provider)


def test_callback_codes_are_removed_from_access_logs():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        "%s %s %s %s %s",
        ("address", "GET", "/api/auth/github/callback?code=private-code&state=private-state", "1.1", 303),
        None,
    )
    assert PrivateQueryFilter().filter(record)
    assert "private-code" not in record.getMessage() and "private-state" not in record.getMessage()


def test_all_roles_accept_enabled_telegram_without_distributing_token_to_workers():
    for role in ["engine", "research"]:
        Settings(runtime_role=role, auth_mode="github", telegram_enabled=True)
    with pytest.raises(ValueError):
        Settings(runtime_role="api", auth_mode="github")
