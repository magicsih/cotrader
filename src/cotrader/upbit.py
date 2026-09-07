"""Upbit KRW/USDT quotes, account reads and explicitly enabled limit orders."""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from urllib.parse import unquote, urlencode
from uuid import uuid4

import httpx

from cotrader.broker import BrokerError
from cotrader.domain import D, Quote
from cotrader.markets import (
    UPBIT_CAUTION_LABELS,
    currency,
    is_upbit,
    order_size_valid,
    price_tick,
    round_quantity,
    upbit_venue,
    validate_symbol,
)


def market_eligible(market, allowed_cautions=()):
    event = market.get("market_event") if isinstance(market, dict) else None
    if not isinstance(event, dict):
        return False
    caution = event.get("caution")
    return (
        event.get("warning") is False
        and isinstance(caution, dict)
        and bool(caution)
        and set(allowed_cautions) <= UPBIT_CAUTION_LABELS.keys()
        and all(
            value is False or (value is True and name in allowed_cautions) for name, value in caution.items()
        )
    )


def account_jwt(access_key: str, secret_key: str, params=None) -> str:
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=")

    payload = {"access_key": access_key, "nonce": str(uuid4())}
    if params:
        query = unquote(urlencode(params, doseq=True))
        payload.update(query_hash=hashlib.sha512(query.encode()).hexdigest(), query_hash_alg="SHA512")
    unsigned = encode({"alg": "HS512", "typ": "JWT"}) + b"." + encode(payload)
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

    async def request(self, method, path, *, params=None, body=None, private=False):
        testing = method == "POST" and path == "/v1/orders/test" and private
        mutation = method != "GET" and not testing
        if mutation and not (self.settings.upbit_live_enabled or self.settings.upbit_usdt_live_enabled):
            raise BrokerError("upbit-read-only")
        if (
            mutation
            and body
            and body.get("market")
            and not self.settings.live_for(upbit_venue(body["market"]))
        ):
            raise BrokerError("upbit-market-read-only")
        allowed = {
            ("GET", "/v1/accounts"),
            ("GET", "/v1/orders/chance"),
            ("GET", "/v1/orders/open"),
            ("GET", "/v1/order"),
            ("POST", "/v1/orders"),
            ("POST", "/v1/orders/test"),
            ("DELETE", "/v1/order"),
        }
        if private and (method, path) not in allowed or not private and method != "GET":
            raise BrokerError("upbit-private-endpoint-disabled")
        headers = {}
        if private:
            access = self.settings.upbit_access_key.get_secret_value()
            secret = self.settings.upbit_secret_key.get_secret_value()
            if not access or not secret:
                raise BrokerError("upbit-credentials-missing")
            headers["Authorization"] = "Bearer " + account_jwt(access, secret, body if body else params)
        async with self.lock:
            await asyncio.sleep(max(0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + 0.2
            try:
                response = await self.client.request(method, path, params=params, json=body, headers=headers)
                if response.status_code in {418, 429}:
                    self.next_request = time.monotonic() + 60
                    raise BrokerError("upbit-rate-limited", ambiguous=mutation)
                if not response.is_success:
                    # Only provider error codes, never bodies, keys or account values in logs.
                    code = response.json().get("error", {}).get("name", "")
                    code = code if isinstance(code, str) and code.replace("_", "").isalnum() else "error"
                    raise BrokerError(f"upbit-{code}", ambiguous=mutation and response.status_code >= 500)
                return response.json()
            except (httpx.HTTPError, ValueError):
                raise BrokerError("upbit-unavailable", ambiguous=mutation) from None

    async def chance(self, symbol):
        upbit_venue(symbol)
        return await self.request("GET", "/v1/orders/chance", params={"market": symbol}, private=True)

    async def orders(self, symbol=None):
        rows, seen = [], set()
        for page in range(1, 101):
            params = {"states[]": ["wait", "watch"], "page": page, "limit": 100, "order_by": "asc"}
            if symbol:
                upbit_venue(symbol)
                params["market"] = symbol
            batch = await self.request("GET", "/v1/orders/open", params=params, private=True)
            if not isinstance(batch, list):
                raise BrokerError("upbit-invalid-orders")
            for row in batch:
                if row["uuid"] in seen:
                    raise BrokerError("upbit-order-pagination-changed")
                seen.add(row["uuid"])
                rows.append(
                    {
                        "orderId": row["uuid"],
                        "symbol": row["market"],
                        "identifier": row.get("identifier"),
                        "side": {"bid": "BUY", "ask": "SELL"}.get(row.get("side")),
                        "price": row.get("price"),
                    }
                )
            if len(batch) < 100:
                return rows
        raise BrokerError("upbit-order-history-too-large")

    async def order(self, order_id=None, *, identifier=None):
        if bool(order_id) == bool(identifier):
            raise ValueError("주문 UUID 또는 식별자 중 하나가 필요합니다")
        raw = await self.request(
            "GET",
            "/v1/order",
            private=True,
            params={"uuid": order_id} if order_id else {"identifier": identifier},
        )
        return normalize_order(raw)

    def order_body(self, intent, *, maker_only=False):
        validate_symbol(intent.venue, intent.symbol)
        if (
            not is_upbit(intent.venue)
            or intent.side not in {"BUY", "SELL"}
            or not order_size_valid(intent.quantity, intent.price, intent.venue)
            or intent.quantity != round_quantity(intent.quantity, intent.venue)
            or intent.price != price_tick(intent.price, intent.venue)
        ):
            raise ValueError("업비트 지정가 주문의 시장·방향·수량·호가 단위를 확인하세요")
        return {
            "market": intent.symbol,
            "side": "bid" if intent.side == "BUY" else "ask",
            "volume": format(intent.quantity.normalize(), "f"),
            "price": format(intent.price.normalize(), "f"),
            "ord_type": "limit",
            "identifier": intent.id,
            **({"time_in_force": "post_only"} if maker_only else {"smp_type": "cancel_taker"}),
        }

    async def test_order(self, intent, *, maker_only=False):
        await self.request(
            "POST", "/v1/orders/test", body=self.order_body(intent, maker_only=maker_only), private=True
        )
        # A test UUID is never persisted as an actual order.
        return {"validated": True, "actual_order_created": False}

    async def place(self, intent, *, maker_only=False):
        raw = await self.request(
            "POST", "/v1/orders", body=self.order_body(intent, maker_only=maker_only), private=True
        )
        if not isinstance(raw, dict) or not raw.get("uuid") or raw.get("identifier") != intent.id:
            raise BrokerError("upbit-invalid-submission", ambiguous=True)
        return {"orderId": raw["uuid"]}

    async def cancel(self, order_id):
        return await self.request("DELETE", "/v1/order", params={"uuid": order_id}, private=True)

    async def markets(self):
        rows = await self.request("GET", "/v1/market/all", params={"is_details": "true"})
        return {r["market"]: r for r in rows if r["market"].startswith(("KRW-", "USDT-"))}

    async def orderbooks(self, symbols):
        if not symbols:
            return []
        for symbol in symbols:
            upbit_venue(symbol)
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
        venue = upbit_venue(symbol)
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
                "currency": currency(venue),
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

    async def account_snapshot(self, venue="upbit"):
        rows = await self.request("GET", "/v1/accounts", private=True)
        if not isinstance(rows, list):
            raise BrokerError("upbit-invalid-account-response")
        assets = [
            {k: r[k] for k in ("currency", "balance", "locked", "avg_buy_price", "unit_currency")}
            for r in rows
        ]
        cur = currency(venue)
        cash = next((r for r in assets if r["currency"] == cur), {"balance": "0", "locked": "0"})
        return {
            "venue": venue,
            "currency": cur,
            "cash_available": cash["balance"],
            "cash_locked": cash["locked"],
            "balances": assets,
            "assets": [r for r in assets if r["currency"] != "KRW"],
            "checked_at": datetime.now(UTC).isoformat(),
        }


def normalize_order(raw):
    """Normalize only complete REST execution evidence; never infer fill amount from limit price."""
    try:
        quantity, filled, fee = D(raw["volume"]), D(raw["executed_volume"]), D(raw["paid_fee"])
        trades = raw.get("trades", [])
        trade_quantity = sum((D(t["volume"]) for t in trades), D(0))
        amount = sum((D(t["funds"]) for t in trades), D(0))
        if (
            any(not n.is_finite() or n < 0 for n in (quantity, filled, fee, amount))
            or quantity <= 0
            or filled > quantity
            or trade_quantity != filled
            or filled > 0
            and amount <= 0
        ):
            raise ValueError("incomplete-execution")
        status = {
            "wait": "PARTIAL_FILLED" if filled else "PENDING",
            "watch": "PENDING",
            "done": "FILLED",
            "cancel": "CANCELED",
            "prevented": "CANCELED",
        }[raw["state"]]
        if status == "FILLED" and filled != quantity:
            raise ValueError("incomplete-terminal-order")
        return {
            "orderId": raw["uuid"],
            "clientOrderId": raw.get("identifier"),
            "symbol": raw["market"],
            "side": {"bid": "BUY", "ask": "SELL"}[raw["side"]],
            "quantity": str(quantity),
            "price": raw["price"],
            "status": status,
            "execution": {
                "filledQuantity": str(filled),
                "filledAmount": str(amount),
                "commission": str(fee),
                "tax": "0",
            },
        }
    except (ValueError, KeyError, TypeError, ArithmeticError):
        raise BrokerError("upbit-invalid-order-evidence") from None
