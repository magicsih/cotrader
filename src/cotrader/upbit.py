"""Upbit public KRW market data and authenticated account reads; no order transport."""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from cotrader.broker import BrokerError
from cotrader.domain import D, Quote
from cotrader.markets import validate_symbol


def account_jwt(access_key: str, secret_key: str) -> str:
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=")

    unsigned = (
        encode({"alg": "HS512", "typ": "JWT"})
        + b"."
        + encode({"access_key": access_key, "nonce": str(uuid4())})
    )
    signature = hmac.new(secret_key.encode(), unsigned, hashlib.sha512).digest()
    return (unsigned + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()


class UpbitBroker:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(base_url="https://api.upbit.com", timeout=15)
        self.lock = asyncio.Lock()
        self.next_request = 0.0

    async def close(self):
        await self.client.aclose()

    async def request(self, method, path, *, params=None, private=False):
        if method != "GET":
            raise BrokerError("upbit-read-only")
        if private and path != "/v1/accounts":
            raise BrokerError("upbit-private-endpoint-disabled")
        headers = {}
        if private:
            access = self.settings.upbit_access_key.get_secret_value()
            secret = self.settings.upbit_secret_key.get_secret_value()
            if not access or not secret:
                raise BrokerError("upbit-credentials-missing")
            headers["Authorization"] = "Bearer " + account_jwt(access, secret)
        async with self.lock:
            await asyncio.sleep(max(0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + 0.2
            try:
                response = await self.client.get(path, params=params, headers=headers)
                if response.status_code in {418, 429}:
                    self.next_request = time.monotonic() + 60
                    raise BrokerError("upbit-rate-limited")
                if response.status_code != 200:
                    raise BrokerError(f"upbit-http-{response.status_code}")
                return response.json()
            except (httpx.HTTPError, ValueError):
                raise BrokerError("upbit-unavailable") from None

    async def markets(self):
        rows = await self.request("GET", "/v1/market/all", params={"is_details": "true"})
        return {r["market"]: r for r in rows if r["market"].startswith("KRW-")}

    async def orderbooks(self, symbols):
        if not symbols:
            return []
        for symbol in symbols:
            validate_symbol("upbit", symbol)
        rows = await self.request("GET", "/v1/orderbook", params={"markets": ",".join(symbols)})
        return [
            Quote(
                r["market"],
                D(str(r["orderbook_units"][0]["bid_price"])),
                D(str(r["orderbook_units"][0]["ask_price"])),
                datetime.fromtimestamp(r["timestamp"] / 1000, UTC),
                D(str(r["orderbook_units"][0]["bid_size"])),
                D(str(r["orderbook_units"][0]["ask_size"])),
            )
            for r in rows
            if r.get("orderbook_units") and r["market"] in symbols
        ]

    async def candles(self, symbol, interval="1m", before=None):
        validate_symbol("upbit", symbol)
        if interval not in {"1m", "1d"}:
            raise ValueError("1m 또는 1d만 지원합니다")
        path = "/v1/candles/minutes/1" if interval == "1m" else "/v1/candles/days"
        params = {"market": symbol, "count": 200}
        if before:
            params["to"] = before
        rows = await self.request("GET", path, params=params)
        candles = [
            {
                "timestamp": r["candle_date_time_utc"] + "+00:00",
                "currency": "KRW",
                **dict(
                    zip(
                        ("openPrice", "highPrice", "lowPrice", "closePrice", "volume"),
                        (
                            str(r[k])
                            for k in (
                                "opening_price",
                                "high_price",
                                "low_price",
                                "trade_price",
                                "candle_acc_trade_volume",
                            )
                        ),
                        strict=True,
                    )
                ),
            }
            for r in rows
        ]
        # The `to` boundary is exclusive; absent trade minutes are never fabricated.
        return {"candles": candles, "nextBefore": min((r["timestamp"] for r in candles), default=None)}

    async def account_snapshot(self):
        rows = await self.request("GET", "/v1/accounts", private=True)
        if not isinstance(rows, list):
            raise BrokerError("upbit-invalid-account-response")
        assets = [
            {k: r[k] for k in ("currency", "balance", "locked", "avg_buy_price", "unit_currency")}
            for r in rows
        ]
        krw = next((r for r in assets if r["currency"] == "KRW"), {"balance": "0", "locked": "0"})
        return {
            "venue": "upbit",
            "cash_available": krw["balance"],
            "cash_locked": krw["locked"],
            "assets": [r for r in assets if r["currency"] != "KRW"],
            "checked_at": datetime.now(UTC).isoformat(),
        }
