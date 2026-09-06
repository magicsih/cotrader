import asyncio
import json
import random
import time
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import websockets

from cotrader.config import Settings
from cotrader.domain import Quote


class BrokerError(Exception):
    def __init__(self, code: str, ambiguous: bool = False):
        super().__init__(code)
        self.code, self.ambiguous = code, ambiguous


class TossBroker:
    base_url = "https://openapi.tossinvest.com"
    ws_url = "wss://openapi-ws.tossinvest.com/ws/v1"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(base_url=self.base_url, timeout=15)
        self.token = ""
        self.expires = 0.0
        self.auth_lock = asyncio.Lock()
        self.limits: dict[str, tuple[float, float]] = {}
        self.limit_lock = asyncio.Lock()

    async def close(self):
        await self.client.aclose()

    async def access_token(self):
        async with self.auth_lock:
            if self.token and time.monotonic() < self.expires:
                return self.token
            if (
                not self.settings.toss_client_id.get_secret_value()
                or not self.settings.toss_client_secret.get_secret_value()
            ):
                raise BrokerError("credentials-missing")
            try:
                response = await self.client.post(
                    "/oauth2/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.settings.toss_client_id.get_secret_value(),
                        "client_secret": self.settings.toss_client_secret.get_secret_value(),
                    },
                )
                if response.status_code != 200:
                    raise BrokerError(f"authentication-http-{response.status_code}")
                data = response.json()
                self.token = data["access_token"]
                self.expires = time.monotonic() + max(1, int(data["expires_in"]) - 120)
                return self.token
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                raise BrokerError("authentication-unavailable") from exc

    async def throttle(self, group):
        async with self.limit_lock:
            rate, next_at = self.limits.get(group, (1.0, 0.0))
            delay = max(0.0, next_at - time.monotonic())
            self.limits[group] = (rate, time.monotonic() + delay + 1 / rate)
        if delay:
            await asyncio.sleep(delay)

    async def request(
        self, method: str, path: str, *, group="ORDER_INFO", params=None, body=None, account=False
    ):
        # Guard the transport as well as place/cancel, including future mutation endpoints.
        if method != "GET" and not self.settings.live_enabled:
            raise BrokerError("live-disabled")
        for attempt in range(3 if method == "GET" else 1):
            await self.throttle(group)
            token = await self.access_token()
            headers = {"Authorization": f"Bearer {token}"}
            if account:
                if self.settings.account_seq is None:
                    raise BrokerError("account-not-selected")
                headers["X-Tossinvest-Account"] = str(self.settings.account_seq)
            try:
                response = await self.client.request(method, path, params=params, json=body, headers=headers)
            except httpx.HTTPError as exc:
                raise BrokerError("transport-unavailable", ambiguous=method != "GET") from exc
            try:
                rate = max(0.1, min(20, float(response.headers.get("X-RateLimit-Limit", "1"))) * 0.8)
                _, next_at = self.limits.get(group, (rate, 0))
                self.limits[group] = (rate, next_at)
                payload = response.json()
            except (ValueError, TypeError) as exc:
                raise BrokerError("invalid-response", ambiguous=method != "GET") from exc
            if response.is_success:
                if "result" not in payload:
                    raise BrokerError("missing-result", ambiguous=method != "GET")
                return payload["result"]
            code = payload.get("error", {}).get("code", f"http-{response.status_code}")
            if response.status_code == 401 and code in {"expired-token", "invalid-token"}:
                # Another request may already have refreshed the cached token.
                if self.token == token:
                    self.expires = 0
                if method == "GET" and attempt < 2:
                    continue
            if method == "GET" and attempt < 2:
                if response.status_code == 429:
                    await asyncio.sleep(
                        min(60, max(1, float(response.headers.get("Retry-After", "1")))) + random.random()
                    )
                    continue
            raise BrokerError(
                code,
                ambiguous=method != "GET" and (response.status_code >= 500 or code == "request-in-progress"),
            )
        raise BrokerError("retry-exhausted")

    async def accounts(self):
        return await self.request("GET", "/api/v1/accounts", group="ACCOUNT")

    async def stocks(self, symbols: list[str]):
        return await self.request(
            "GET", "/api/v1/stocks", group="STOCK", params={"symbols": ",".join(symbols)}
        )

    async def calendar(self):
        return await self.request("GET", "/api/v1/market-calendar/US", group="MARKET_INFO")

    async def holdings(self):
        return await self.request("GET", "/api/v1/holdings", group="ASSET", account=True)

    async def buying_power(self, currency="USD"):
        data = await self.request("GET", "/api/v1/buying-power", params={"currency": currency}, account=True)
        return Decimal(data["cashBuyingPower"])

    async def account_snapshot(self):
        accounts = [r for r in await self.accounts() if r.get("accountType") == "BROKERAGE"]
        if self.settings.account_seq is None:
            if len(accounts) != 1:
                raise BrokerError("account-selection-required" if accounts else "account-unavailable")
            self.settings.account_seq = accounts[0]["accountSeq"]
        account = next((r for r in accounts if r["accountSeq"] == self.settings.account_seq), None)
        if account is None:
            raise BrokerError("account-not-found")
        usd = await self.buying_power("USD")
        krw = await self.buying_power("KRW")
        holdings = await self.holdings()
        orders = await self.orders()
        rate = await self.commission_rate()
        return {
            "account_mask": "••••" + str(account["accountNo"])[-4:],
            "account_type": account["accountType"],
            "cash_buying_power": {"USD": str(usd), "KRW": str(krw)},
            "holdings": holdings,
            "open_orders": [
                {
                    key: row.get(key)
                    for key in ("symbol", "side", "quantity", "filledQuantity", "price", "status")
                }
                for row in orders
            ],
            "us_commission_rate": str(rate),
            "checked_at": datetime.now(UTC).isoformat(),
        }

    async def commission_rate(self):
        rows = await self.request("GET", "/api/v1/commissions", account=True)
        for row in rows:
            if row.get("marketCountry") == "US":
                return Decimal(row["commissionRate"])
        raise BrokerError("us-commission-unavailable")

    async def sellable(self, symbol):
        data = await self.request("GET", "/api/v1/sellable-quantity", params={"symbol": symbol}, account=True)
        return Decimal(data["sellableQuantity"])

    async def orders(self, status="OPEN", **params):
        rows, cursor = [], None
        for _ in range(1000):
            data = await self.request(
                "GET",
                "/api/v1/orders",
                group="ORDER_HISTORY",
                account=True,
                params={"status": status, "limit": 100, **params, **({"cursor": cursor} if cursor else {})},
            )
            rows.extend(data["orders"])
            new_cursor = data.get("nextCursor")
            if status == "OPEN" or not new_cursor:
                return rows
            if new_cursor == cursor:
                raise BrokerError("order-pagination-stalled")
            cursor = new_cursor
        raise BrokerError("order-history-too-large")

    async def order(self, order_id):
        return await self.request("GET", f"/api/v1/orders/{order_id}", group="ORDER_HISTORY", account=True)

    async def place(self, intent):
        if not self.settings.live_enabled:
            raise BrokerError("live-disabled")
        if (
            not intent.quantity.is_finite()
            or intent.quantity <= 0
            or intent.quantity != intent.quantity.to_integral_value()
        ):
            raise ValueError("토스 실거래는 양의 정수 수량만 지원합니다")
        result = await self.request(
            "POST",
            "/api/v1/orders",
            group="ORDER",
            account=True,
            body={
                "clientOrderId": intent.id,
                "symbol": intent.symbol,
                "side": intent.side,
                "orderType": "LIMIT",
                "timeInForce": "DAY",
                "quantity": str(int(intent.quantity)),
                "price": str(intent.price),
            },
        )
        if not isinstance(result, dict) or not result.get("orderId"):
            raise BrokerError("invalid-submission", ambiguous=True)
        return result

    async def cancel(self, order_id):
        if not self.settings.live_enabled:
            raise BrokerError("live-disabled")
        return await self.request(
            "POST", f"/api/v1/orders/{order_id}/cancel", group="ORDER", account=True, body={}
        )

    async def candles(self, symbol, interval="1m", before=None):
        return await self.request(
            "GET",
            "/api/v1/candles",
            group="MARKET_DATA_CHART",
            params={
                "symbol": symbol,
                "interval": interval,
                "count": 200,
                "adjusted": "false",
                **({"before": before} if before else {}),
            },
        )

    async def orderbook(self, symbol):
        data = await self.request("GET", "/api/v1/orderbook", group="MARKET_DATA", params={"symbol": symbol})
        return parse_quote(symbol, data)

    async def stream(self, symbols, on_quote, on_order, on_reconnect):
        delay = 1
        while True:
            try:
                token = await self.access_token()
                async with websockets.connect(
                    self.ws_url,
                    additional_headers={"Authorization": f"Bearer {token}"},
                    ping_interval=60,
                    ping_timeout=20,
                    max_queue=1000,
                ) as socket:
                    declarations = [{"type": "orderbook:us", "codes": symbols}] if symbols else []
                    if self.settings.account_seq is not None:
                        declarations.append(
                            {"type": "personal:order", "codes": [str(self.settings.account_seq)]}
                        )
                    await socket.send(json.dumps(declarations))
                    await on_reconnect()
                    async for raw in socket:
                        message = json.loads(raw)
                        if message.get("type") == "subscriptions":
                            if message.get("rejected"):
                                raise BrokerError("subscription-rejected")
                            delay = 1
                        elif message.get("type") == "error":
                            raise BrokerError("stream-server-error")
                        elif message.get("type") == "message":
                            topic = message.get("topic", "")
                            if topic.startswith("orderbook:us:"):
                                quote = parse_quote(topic.split(":", 2)[2], message["data"])
                                if quote:
                                    on_quote(quote)
                            elif topic.startswith("personal:order:"):
                                on_order(message["data"]["order"])
            except asyncio.CancelledError:
                raise
            except (BrokerError, OSError, websockets.WebSocketException, ValueError, KeyError):
                await on_reconnect()
                await asyncio.sleep(delay + random.random())
                delay = min(delay * 2, 30)


def parse_quote(symbol: str, data: dict) -> Quote | None:
    if data.get("currency") != "USD" or not data.get("timestamp"):
        return None
    asks = [r for r in data.get("asks", []) if Decimal(r["volume"]) > 0 and Decimal(r["price"]) > 0]
    bids = [r for r in data.get("bids", []) if Decimal(r["volume"]) > 0 and Decimal(r["price"]) > 0]
    if not asks or not bids:
        return None
    ask, bid = min(asks, key=lambda r: Decimal(r["price"])), max(bids, key=lambda r: Decimal(r["price"]))
    return Quote(
        symbol,
        Decimal(bid["price"]),
        Decimal(ask["price"]),
        datetime.fromisoformat(data["timestamp"]).astimezone(UTC),
        Decimal(bid["volume"]),
        Decimal(ask["volume"]),
    )


def current_session(calendar: dict, at: datetime) -> tuple[str, str] | None:
    for day in calendar.values():
        if not isinstance(day, dict) or "date" not in day:
            continue
        for name in ("dayMarket", "preMarket", "regularMarket", "afterMarket"):
            session = day.get(name)
            if session and datetime.fromisoformat(session["startTime"]) <= at < datetime.fromisoformat(
                session["endTime"]
            ):
                return name, day["date"]
    return None
