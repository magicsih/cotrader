import base64
import hashlib
import hmac
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete

from cotrader.auth import session_token
from cotrader.models import LoginChallenge, now

STATE_COOKIE = "__Host-cotrader_oauth"
CALLBACK_PATH = "/api/auth/github/callback"


def sha256(value):
    return hashlib.sha256(value.encode()).hexdigest()


def pkce_challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


async def github_identity(settings, code, verifier, client=None):
    owned = client is None
    client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
    try:
        response = await client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": settings.github_client_id.get_secret_value(),
                "client_secret": settings.github_client_secret.get_secret_value(),
                "code": code,
                "redirect_uri": settings.public_url + CALLBACK_PATH,
                "code_verifier": verifier,
            },
        )
        data = response.json()
        if response.status_code != 200 or not data.get("access_token") or data.get("error"):
            raise ValueError("GitHub 인증 실패")
        # A dedicated identity-only application must not inherit repository or organization permissions.
        if data.get("scope", "") or data.get("token_type", "").lower() != "bearer":
            raise ValueError("GitHub 로그인 앱에 불필요한 권한이 부여되어 있습니다")
        response = await client.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {data['access_token']}",
                "Accept": "application/vnd.github+json",
            },
        )
        profile = response.json()
        if (
            response.status_code != 200
            or type(profile.get("id")) is not int
            or profile["id"] != settings.github_user_id
        ):
            raise ValueError("허용되지 않은 GitHub 계정입니다")
        return profile["id"]
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        raise ValueError("GitHub 인증 또는 허용 사용자 확인에 실패했습니다") from None
    finally:
        if owned:
            await client.aclose()


def github_routes(settings, sessions):
    router = APIRouter()

    @router.get("/api/auth/github")
    async def login():
        if settings.auth_mode != "github":
            raise HTTPException(404, "GitHub 로그인을 사용하지 않습니다")
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        async with sessions.begin() as session:
            await session.execute(delete(LoginChallenge).where(LoginChallenge.expires_at <= now()))
            session.add(
                LoginChallenge(id=sha256(state), verifier=verifier, expires_at=now() + timedelta(minutes=5))
            )
        query = urlencode(
            {
                "client_id": settings.github_client_id.get_secret_value(),
                "redirect_uri": settings.public_url + CALLBACK_PATH,
                "scope": "",
                "state": state,
                "code_challenge": pkce_challenge(verifier),
                "code_challenge_method": "S256",
                "allow_signup": "false",
            }
        )
        response = RedirectResponse("https://github.com/login/oauth/authorize?" + query, status_code=303)
        response.set_cookie(
            STATE_COOKIE, state, max_age=300, secure=True, httponly=True, samesite="lax", path="/"
        )
        return response

    @router.get(CALLBACK_PATH)
    async def callback(request: Request):
        if settings.auth_mode != "github":
            raise HTTPException(404, "GitHub 로그인을 사용하지 않습니다")
        state, code = request.query_params.get("state", ""), request.query_params.get("code", "")
        browser_state = request.cookies.get(STATE_COOKIE, "")
        if not state or len(state) > 128 or not hmac.compare_digest(state, browser_state):
            raise HTTPException(401, "로그인 요청 확인에 실패했습니다")
        async with sessions.begin() as session:
            challenge = await session.get(LoginChallenge, sha256(state), with_for_update=True)
            if not challenge or challenge.expires_at <= now():
                raise HTTPException(401, "로그인 요청이 만료되었거나 이미 처리되었습니다")
            verifier = challenge.verifier
            await session.delete(challenge)
        response = RedirectResponse(settings.public_url + "/?login_error=github", status_code=303)
        response.delete_cookie(STATE_COOKIE, secure=True, httponly=True, samesite="lax", path="/")
        if request.query_params.get("error") or not code or len(code) > 512:
            return response
        try:
            user = await github_identity(settings, code, verifier)
        except ValueError:
            return response
        response.headers["location"] = settings.public_url + "/"
        response.set_cookie(
            "cotrader_session",
            session_token(user, settings.session_secret.get_secret_value(), provider="github"),
            max_age=3600,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    return router
