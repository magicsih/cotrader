from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from cotrader.markets import currency


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COTRADER_", extra="ignore", hide_input_in_errors=True)
    runtime_role: Literal["api", "engine", "research"] = "api"
    database_url: SecretStr = SecretStr(
        "mysql+asyncmy://cotrader:local-development-only@127.0.0.1:13316/cotrader"
    )
    auth_mode: Literal["local", "telegram", "github"] = "local"
    public_url: str = "http://127.0.0.1:8000"
    market_source: Literal["offline", "toss"] = "offline"
    live_enabled: bool = False
    upbit_live_enabled: bool = False
    upbit_usdt_live_enabled: bool = False
    account_reads_enabled: bool = False
    telegram_enabled: bool = False
    upbit_enabled: bool = False
    upbit_account_reads_enabled: bool = False
    upbit_account_label: str = Field(default="업비트 연결 계좌", min_length=1, max_length=60)
    capital_krw: Decimal = Field(default=Decimal("1000000"), gt=0, le=10000000)
    daily_loss_krw: Decimal = Field(default=Decimal("10000"), gt=0)
    drawdown_krw: Decimal = Field(default=Decimal("50000"), gt=0)
    capital_usdt: Decimal = Field(default=Decimal("500"), gt=0, le=10000)
    daily_loss_usdt: Decimal = Field(default=Decimal("5"), gt=0)
    drawdown_usdt: Decimal = Field(default=Decimal("25"), gt=0)
    upbit_usdt_auto_recover: bool = False
    risk_recovery_ratio: Decimal = Field(default=Decimal("0.8"), gt=0, lt=1)
    risk_recovery_seconds: int = Field(default=60, ge=60, le=3600)
    upbit_access_key: SecretStr = Field(default=SecretStr(""), validation_alias="UPBIT_OPEN_API_ACCESS_KEY")
    upbit_secret_key: SecretStr = Field(default=SecretStr(""), validation_alias="UPBIT_OPEN_API_SECRET_KEY")
    capital_usd: Decimal = Field(default=Decimal("5000"), gt=0)
    daily_loss_usd: Decimal = Field(default=Decimal("50"), gt=0)
    drawdown_usd: Decimal = Field(default=Decimal("250"), gt=0)
    session_secret: SecretStr = SecretStr("")
    github_client_id: SecretStr = SecretStr("")
    github_client_secret: SecretStr = SecretStr("")
    github_user_id: int = Field(default=0, ge=0)
    account_seq: int | None = None
    toss_client_id: SecretStr = Field(
        default=SecretStr(""), validation_alias="TOSS_INVEST_OPEN_API_CLIENT_ID"
    )
    toss_client_secret: SecretStr = Field(
        default=SecretStr(""), validation_alias="TOSS_INVEST_OPEN_API_CLIENT_SECRET"
    )
    telegram_token: SecretStr = Field(default=SecretStr(""), validation_alias="TELEGRAM_API_KEY")
    telegram_me: int = Field(default=0, validation_alias="TELEGRAM_ME")
    discovery_collection_seconds: int = Field(default=1800, ge=60, le=7200)
    discovery_compute_seconds: int = Field(default=1200, ge=30, le=3600)
    static_dir: str = "web/dist"

    def live_for(self, venue):
        if venue == "upbit_usdt":
            return self.upbit_usdt_live_enabled
        return self.upbit_live_enabled if venue == "upbit" else self.live_enabled

    def capital_for(self, venue):
        return getattr(self, f"capital_{currency(venue).lower()}")

    def risk_for(self, venue):
        return {
            "daily_loss": str(getattr(self, f"daily_loss_{currency(venue).lower()}")),
            "drawdown": str(getattr(self, f"drawdown_{currency(venue).lower()}")),
        }

    def auto_recover_for(self, venue):
        return venue == "upbit_usdt" and self.upbit_usdt_auto_recover

    @model_validator(mode="after")
    def secure_configuration(self):
        if (
            self.telegram_enabled
            and self.runtime_role == "api"
            and (not self.telegram_me or not self.telegram_token.get_secret_value())
        ):
            raise ValueError("Telegram 연결에는 봇 토큰과 허용 사용자 ID가 필요합니다")
        if self.auth_mode == "telegram" and self.runtime_role == "api":
            if not self.telegram_me or not self.telegram_token.get_secret_value():
                raise ValueError("Telegram 인증에는 봇 토큰과 허용 사용자 ID가 필요합니다")
        if self.auth_mode == "github" and self.runtime_role == "api":
            if (
                not self.github_user_id
                or not self.github_client_id.get_secret_value()
                or not self.github_client_secret.get_secret_value()
            ):
                raise ValueError("GitHub 인증에는 OAuth 앱과 허용 사용자 ID가 필요합니다")
        if self.auth_mode != "local" and self.runtime_role == "api":
            origin = urlsplit(self.public_url)
            if (
                len(self.session_secret.get_secret_value()) < 32
                or origin.scheme != "https"
                or not origin.hostname
                or origin.username
                or origin.password
                or origin.path
                or origin.query
                or origin.fragment
            ):
                raise ValueError("외부 인증에는 경로 없는 HTTPS 주소와 32자 이상의 세션 서명이 필요합니다")
        if self.live_enabled and (self.auth_mode == "local" or self.market_source != "toss"):
            raise ValueError("실거래는 외부 인증 및 Toss 시세 연결에서만 활성화할 수 있습니다")
        if (self.upbit_live_enabled or self.upbit_usdt_live_enabled) and (
            self.auth_mode == "local" or not self.upbit_enabled
        ):
            raise ValueError("업비트 실거래는 외부 인증 및 업비트 시세 연결에서만 활성화할 수 있습니다")
        return self
