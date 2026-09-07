import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import func, select

from cotrader.account import account_view
from cotrader.api import create_app
from cotrader.broker import BrokerError, TossBroker
from cotrader.config import Settings
from cotrader.engine import Engine
from cotrader.models import Command, RuntimeState
from cotrader.telegram import TelegramBot
from cotrader.telegram_views import toss


def fixture_settings():
    return Settings(
        TOSS_INVEST_OPEN_API_CLIENT_ID="fixture-client",
        TOSS_INVEST_OPEN_API_CLIENT_SECRET="fixture-secret",
        TELEGRAM_API_KEY="fixture-token",
        TELEGRAM_ME=7,
    )


async def test_readonly_transport_blocks_every_mutation_before_authentication():
    requests = []
    broker = TossBroker(
        fixture_settings(),
        httpx.AsyncClient(
            base_url=TossBroker.base_url, transport=httpx.MockTransport(lambda r: requests.append(r))
        ),
    )
    for method, path in [
        ("POST", "/api/v1/orders"),
        ("POST", "/api/v1/orders/1/cancel"),
        ("DELETE", "/api/v1/conditional-orders/1"),
        ("PATCH", "/future"),
    ]:
        with pytest.raises(BrokerError, match="live-disabled"):
            await broker.request(method, path)
    assert requests == []
    await broker.close()


async def test_account_snapshot_selects_one_account_and_masks_number():
    broker = TossBroker(fixture_settings())
    broker.accounts = AsyncMock(
        return_value=[{"accountType": "BROKERAGE", "accountSeq": 9, "accountNo": "123456789012"}]
    )
    broker.buying_power = AsyncMock(side_effect=["5000", "1000"])
    broker.holdings = AsyncMock(return_value={"items": []})
    broker.orders = AsyncMock(
        return_value=[{"symbol": "AAPL", "side": "BUY", "accountNo": "sensitive", "orderId": "unneeded"}]
    )
    broker.commission_rate = AsyncMock(return_value="0.001")
    data = await broker.account_snapshot()
    assert broker.settings.account_seq == 9
    assert data["account_mask"] == "••••9012"
    assert "123456789012" not in str(data) and "sensitive" not in str(data) and "unneeded" not in str(data)
    broker.settings.account_seq = None
    broker.accounts.return_value *= 2
    with pytest.raises(BrokerError, match="account-selection-required"):
        await broker.account_snapshot()
    await broker.close()


async def test_account_read_failure_preserves_previous_snapshot_without_claiming_current(db):
    engine = Engine(fixture_settings())
    await engine.db.dispose()
    engine.db, engine.sessions = db
    good = {
        "checked_at": datetime.now(UTC).isoformat(),
        "cash_buying_power": {"USD": "123", "KRW": "0"},
        "account_mask": "••••1234",
        "holdings": {"items": []},
        "open_orders": [],
        "us_commission_rate": "0.001",
    }
    engine.broker.account_snapshot = AsyncMock(side_effect=[good, BrokerError("authentication-http-403")])
    await engine.refresh_account(datetime.now(UTC))
    await engine.refresh_account(datetime.now(UTC))
    async with engine.sessions() as session:
        row = await session.get(RuntimeState, "broker_account")
        view = account_view(row)
        assert view["status"] == "ERROR" and view["snapshot"] == good and view["read_only"]
        assert "마지막 조회 값" in str(toss(view))
        assert await session.scalar(select(func.count()).select_from(Command)) == 0
    await engine.broker.close()


def test_stale_and_missing_account_values_are_not_zero_balances():
    assert account_view(None)["snapshot"] is None
    old = {"checked_at": (datetime.now(UTC) - timedelta(minutes=3)).isoformat()}
    assert account_view(SimpleNamespace(data={"status": "CONNECTED", "snapshot": old}))["stale"]


async def test_telegram_account_is_private_and_does_not_create_trade_commands(db):
    database, sessions = db
    bot = TelegramBot(fixture_settings(), sessions, database)
    bot.send = AsyncMock()
    for sender, chat, kind in [(8, 7, "private"), (7, 8, "private"), (7, 7, "group")]:
        await bot.handle(
            {
                "update_id": 1,
                "message": {"from": {"id": sender}, "chat": {"id": chat, "type": kind}, "text": "/account"},
            }
        )
    bot.send.assert_not_called()
    await bot.handle(
        {
            "update_id": 2,
            "message": {"from": {"id": 7}, "chat": {"id": 7, "type": "private"}, "text": "/account"},
        }
    )
    assert "아직 확인된 계좌" in str(bot.send.call_args.args[0])
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Command)) == 0
    bot.send.reset_mock()
    await bot.handle(
        {
            "update_id": 3,
            "message": {
                "date": int(time.time()),
                "from": {"id": 7},
                "chat": {"id": 7, "type": "private"},
                "text": "/web",
            },
        }
    )
    assert "HTTPS" in str(bot.send.call_args.args[0])
    assert bot.send.call_args.args[1]
    await bot.client.aclose()


async def test_account_api_requires_local_auth_and_returns_readonly_status(db):
    _, sessions = db
    app = create_app(Settings(), sessions_override=sessions)
    for host, expected in [("127.0.0.1", 200), ("198.51.100.4", 403)]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(host, 8000)), base_url="http://127.0.0.1:8000"
        ) as client:
            response = await client.get("/api/account")
            assert response.status_code == expected
            if expected == 200:
                assert response.json()["read_only"] is True
                assert response.json()["snapshot"] is None


async def test_loopback_api_rejects_foreign_host_headers(db):
    _, sessions = db
    app = create_app(Settings(), sessions_override=sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 8000)),
        base_url="http://rebinding.example",
    ) as client:
        assert (await client.get("/api/account")).status_code == 403
