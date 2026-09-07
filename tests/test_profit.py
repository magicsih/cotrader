import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import func, select

from cotrader.api import create_app
from cotrader.broker import BrokerError
from cotrader.config import Settings
from cotrader.engine import Engine
from cotrader.models import Command, RuntimeState
from cotrader.profit import build_summary, fetch_fx, refresh_fx
from cotrader.telegram import TelegramBot


def portfolio(gross="0", costs="0", unrealized="0", at=None, **changes):
    data = {
        "realized_gross": gross,
        "costs": costs,
        "unrealized": unrealized,
        "capital": "10000",
        "equity": str(D(10000) + D(gross) - D(costs) + D(unrealized)),
        "complete": True,
        "unresolved_orders": 0,
        **changes,
    }
    return SimpleNamespace(data=data, updated_at=at or datetime.now(UTC))


def rate(value, at=None, **changes):
    at = at or datetime.now(UTC)
    return SimpleNamespace(
        data={
            "rate": value,
            "status": "CONNECTED",
            "checked_at": at.isoformat(),
            "valid_from": (at - timedelta(seconds=10)).isoformat(),
            "valid_until": (at + timedelta(seconds=60)).isoformat(),
            **changes,
        }
    )


def fixtures(at=None):
    return (
        {
            "toss": portfolio("100", "2", "-30", at),
            "upbit": portfolio("1500", "100", "300", at),
            "upbit_usdt": portfolio("8", "1", "3", at),
        },
        {"USD": rate("1300", at), "USDT": rate("1400", at)},
    )


def test_three_markets_convert_costs_and_losses_without_pegging_usdt_to_usd():
    portfolios, rates = fixtures()
    report = build_summary(portfolios, rates)
    assert report["complete"]
    assert report["total"] == {
        "total_net": "104100",
        "realized_net": "138600",
        "unrealized": "-34500",
        "realized_gross": "142700",
        "costs": "4100",
    }
    assert [row["metrics"]["total_net"] for row in report["markets"]] == ["68", "1700", "10"]
    # A changed unallocated operating limit does not alter cumulative profit.
    portfolios["toss"].data.update(capital="90000", equity="90068")
    assert build_summary(portfolios, rates)["total"] == report["total"]


@pytest.mark.parametrize("problem", ["missing", "expired", "future", "failed", "nonfinite"])
def test_unusable_rate_never_becomes_zero_or_a_partial_total(problem):
    at = datetime.now(UTC)
    portfolios, rates = fixtures(at)
    if problem == "missing":
        del rates["USDT"]
    elif problem == "expired":
        rates["USDT"].data["valid_until"] = at.isoformat()
    elif problem == "future":
        rates["USDT"].data["valid_from"] = (at + timedelta(minutes=1)).isoformat()
    elif problem == "nonfinite":
        rates["USDT"].data["rate"] = "NaN"
    else:
        rates["USDT"].data["status"] = "ERROR"
    report = build_summary(portfolios, rates, at=at)
    assert not report["complete"] and report["total"]["total_net"] is None
    assert report["markets"][0]["converted"]["total_net"] == "88400"
    assert report["markets"][2]["metrics"]["total_net"] == "10"
    assert report["markets"][2]["converted"] is None


@pytest.mark.parametrize(
    "problem,status",
    [
        ("missing", "MISSING"),
        ("stale", "STALE"),
        ("quote", "INCOMPLETE"),
        ("orders", "UNRESOLVED"),
        ("ledger", "INCONSISTENT"),
    ],
)
def test_incomplete_portfolio_is_not_omitted_from_total(problem, status):
    at = datetime.now(UTC)
    portfolios, rates = fixtures(at)
    if problem == "missing":
        del portfolios["toss"]
    elif problem == "stale":
        portfolios["toss"].updated_at = at - timedelta(seconds=121)
    elif problem == "quote":
        portfolios["toss"].data.update(complete=False, equity=None, unrealized=None)
    elif problem == "orders":
        portfolios["toss"].data["unresolved_orders"] = 1
    else:
        portfolios["toss"].data["equity"] = "10069"
    report = build_summary(portfolios, rates, at=at)
    assert not report["complete"] and report["total"]["total_net"] is None
    assert report["markets"][0]["status"] == status


def test_known_zero_profit_does_not_require_an_unused_conversion():
    portfolios = {venue: portfolio() for venue in ("toss", "upbit", "upbit_usdt")}
    assert build_summary(portfolios, {})["total"]["total_net"] == "0"
    portfolios["upbit_usdt"] = portfolio("8", "1", "3", closed_unrealized="2")
    report = build_summary(portfolios, {"USDT": rate("1400")})
    # The risk snapshot's unrealized amount already includes closed handover P&L.
    assert report["total"]["total_net"] == "14000"


async def test_reference_mid_rate_and_usdt_orderbook_are_independent_sources():
    at = datetime.now(UTC)
    broker = SimpleNamespace(
        request=AsyncMock(
            return_value={
                "baseCurrency": "USD",
                "quoteCurrency": "KRW",
                "rate": "1390",
                "midRate": "1375",
                "validFrom": (at - timedelta(seconds=5)).isoformat(),
                "validUntil": (at + timedelta(seconds=55)).isoformat(),
            }
        )
    )
    upbit = SimpleNamespace(
        orderbooks=AsyncMock(
            return_value=[SimpleNamespace(symbol="KRW-USDT", bid=D(1400), ask=D(1402), mid=D(1401), at=at)]
        )
    )
    assert (await fetch_fx(broker, upbit, "USD"))["rate"] == "1375"
    assert (await fetch_fx(broker, upbit, "USDT"))["rate"] == "1401"
    broker.request.assert_awaited_once_with(
        "GET",
        "/api/v1/exchange-rate",
        group="MARKET_INFO",
        params={"baseCurrency": "USD", "quoteCurrency": "KRW"},
    )
    upbit.orderbooks.assert_awaited_once_with(["KRW-USDT"])


async def test_failed_provider_keeps_last_evidence_and_does_not_hide_other_currency(db):
    _, sessions = db
    async with sessions.begin() as session:
        session.add(RuntimeState(key="fx:USD:KRW", data=rate("1300").data))
    broker = SimpleNamespace(request=AsyncMock(side_effect=BrokerError("fixture-unavailable")))
    at = datetime.now(UTC)
    upbit = SimpleNamespace(
        orderbooks=AsyncMock(
            return_value=[SimpleNamespace(symbol="KRW-USDT", bid=D(1400), ask=D(1402), mid=D(1401), at=at)]
        )
    )
    await refresh_fx(Settings(market_source="toss", upbit_enabled=True), broker, upbit, sessions)
    async with sessions() as session:
        usd = await session.get(RuntimeState, "fx:USD:KRW")
        usdt = await session.get(RuntimeState, "fx:USDT:KRW")
        assert usd.data["status"] == "ERROR" and usd.data["rate"] == "1300"
        assert usdt.data["status"] == "CONNECTED"


async def test_api_and_telegram_share_read_only_live_profit_and_protect_private_data(db):
    database, sessions = db
    portfolios, rates = fixtures()
    async with sessions.begin() as session:
        for venue, row in portfolios.items():
            session.add(RuntimeState(key=f"portfolio:{venue}:live", data=row.data))
            session.add(RuntimeState(key=f"portfolio:{venue}:paper", data=portfolio().data))
        for unit, row in rates.items():
            session.add(RuntimeState(key=f"fx:{unit}:KRW", data=row.data))
    app = create_app(Settings(), sessions_override=sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5)), base_url="http://localhost"
    ) as client:
        response = await client.get("/api/profit")
        assert response.status_code == 200 and response.json()["total"]["total_net"] == "104100"
        assert (await client.get("/api/profit?mode=paper")).json()["total"]["total_net"] == "0"
        assert (await client.get("/api/profit?mode=all")).status_code == 422
        assert (await client.get("/api/profit?base_currency=BTC")).status_code == 422
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("10.0.0.8", 5)), base_url="http://localhost"
    ) as client:
        assert (await client.get("/api/profit")).status_code == 403
    bot = TelegramBot(Settings(TELEGRAM_API_KEY="fixture-token", TELEGRAM_ME=7), sessions, database)
    bot.send, bot.call = AsyncMock(), AsyncMock()
    await bot.handle(
        {
            "update_id": 99,
            "message": {"from": {"id": 7}, "chat": {"id": 7, "type": "private"}, "text": "/profit"},
        }
    )
    rendered = str(bot.send.call_args.args[0])
    assert "104,100" in rendered and "전체 합산" in rendered and "환산 기준" in rendered
    await bot.handle(
        {
            "update_id": 100,
            "callback_query": {
                "id": "fixture",
                "from": {"id": 7},
                "message": {"message_id": 45, "chat": {"id": 7, "type": "private"}},
                "data": "nav:profit:0",
            },
        }
    )
    assert bot.send.call_args.kwargs["message_id"] == 45
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Command)) == 0
    await bot.client.aclose()


async def test_slow_reporting_fetch_does_not_block_engine_and_is_cancelled_on_shutdown(db, monkeypatch):
    database, sessions = db
    entered, cycled, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    engine = Engine(Settings())
    await engine.db.dispose()
    engine.db, engine.sessions = database, sessions

    class Lock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def verify(self):
            pass

    async def slow_fx(_settings, broker, upbit, _sessions):
        assert broker is engine.broker and upbit is engine.upbit
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    async def cycle():
        await entered.wait()
        cycled.set()
        await asyncio.Future()

    monkeypatch.setattr("cotrader.engine.SingleWriter", lambda *_: Lock())
    monkeypatch.setattr("cotrader.profit.fx_loop", slow_fx)
    engine.cycle = cycle
    task = asyncio.create_task(engine.run())
    try:
        await asyncio.wait_for(cycled.wait(), timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert cancelled.is_set() and engine.broker.client.is_closed and engine.upbit.client.is_closed
