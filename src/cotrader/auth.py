import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from fastapi import HTTPException, Request


def telegram_user(init_data: str, bot_token: str, allowed_user: int, at: int | None = None) -> int:
    pairs = parse_qsl(init_data, keep_blank_values=True)
    if len({k for k, _ in pairs}) != len(pairs):
        raise ValueError("중복 인증 필드")
    data = dict(pairs)
    signature = data.pop("hash", "")
    check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("인증 서명 불일치")
    timestamp = int(data.get("auth_date", "0"))
    age = (int(time.time()) if at is None else at) - timestamp
    if not 0 <= age <= 300:
        raise ValueError("인증 데이터 만료")
    user = json.loads(data["user"])
    if user.get("id") != allowed_user:
        raise ValueError("허용되지 않은 사용자")
    return allowed_user


def session_token(user: int, secret: str, at: int | None = None, provider="telegram") -> str:
    expires = (int(time.time()) if at is None else at) + 3600
    payload = f"{provider}:{user}:{expires}"
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def verify_session(token: str, secret: str, allowed_user: int, at: int | None = None, provider="telegram"):
    try:
        actual_provider, user, expires, signature = token.split(":")
        payload = f"{actual_provider}:{user}:{expires}"
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        remaining = int(expires) - (int(time.time()) if at is None else at)
        if (
            not hmac.compare_digest(signature, expected)
            or actual_provider != provider
            or int(user) != allowed_user
            or not 0 < remaining <= 3600
        ):
            raise ValueError("세션 만료 또는 불일치")
        return int(user)
    except (ValueError, TypeError) as exc:
        raise ValueError("유효하지 않은 세션") from exc


def require_actor(request: Request) -> str:
    settings = request.app.state.settings
    if settings.auth_mode == "local":
        if request.url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise HTTPException(403, "로컬 모드는 loopback 호스트 이름만 허용합니다")
        if not request.client or request.client.host not in {"127.0.0.1", "::1"}:
            raise HTTPException(403, "로컬 모드는 loopback 접속만 허용합니다")
        if request.method not in {"GET", "HEAD"} and request.headers.get("origin") not in {
            None,
            settings.public_url,
        }:
            raise HTTPException(403, "요청 출처가 일치하지 않습니다")
        return "local"
    if request.method not in {"GET", "HEAD"} and request.headers.get("origin") != settings.public_url:
        raise HTTPException(403, "요청 출처가 일치하지 않습니다")
    try:
        user = verify_session(
            request.cookies.get("cotrader_session", ""),
            settings.session_secret.get_secret_value(),
            settings.github_user_id if settings.auth_mode == "github" else settings.telegram_me,
            provider=settings.auth_mode,
        )
        return f"{settings.auth_mode}:{user}"
    except ValueError as exc:
        raise HTTPException(401, "로그인이 필요합니다") from exc
