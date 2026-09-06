import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from cotrader.api import create_app
from cotrader.broker import BrokerError
from cotrader.config import Settings
from cotrader.domain import Bar, D, Quote, StrategySpec, decide, initial_state, levels, slot_quantity
from cotrader.engine import Engine
from cotrader.markets import price_tick
from cotrader.models import AccountState, Intent, RuntimeState, Strategy
from cotrader.research import backtest
from cotrader.services import bootstrap, check_risk, create_strategy
from cotrader.upbit import UpbitBroker, account_jwt


def crypto_spec(**changes):
    return StrategySpec(
        **{
            **dict(
                venue="upbit",
                symbol="KRW-BTC",
                budget="1000000",
                lower="90000000",
                upper="110000000",
                grids=4,
                spacing="arithmetic",
            ),
            **changes,
        }
    )


def test_upbit_auth_uses_raw_hs512_secret_and_unique_nonce():
    token = account_jwt("fixture-access", "fixture-secret")
    header, payload, signature = token.split(".")
    assert json.loads(base64.urlsafe_b64decode(header + "=="))["alg"] == "HS512"
    assert hmac.compare_digest(
        base64.urlsafe_b64decode(signature + "=="),
        hmac.new(b"fixture-secret", f"{header}.{payload}".encode(), hashlib.sha512).digest(),
    )
    assert token != account_jwt("fixture-access", "fixture-secret")


async def test_upbit_transport_has_no_order_or_withdrawal_path():
    calls = []

    async def responder(request):
        calls.append(request)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(
        base_url="https://api.upbit.com", transport=httpx.MockTransport(responder)
    ) as client:
        broker = UpbitBroker(Settings(), client)
        for method in ("POST", "DELETE", "PUT"):
            with pytest.raises(BrokerError, match="read-only"):
                await broker.request(method, "/v1/orders")
        with pytest.raises(BrokerError, match="endpoint-disabled"):
            await broker.request("GET", "/v1/withdraws", private=True)
        assert calls == []


async def test_upbit_candles_keep_real_gaps_and_exclusive_cursor():
    async def responder(request):
        assert request.url.params["to"] == "2026-09-01T00:03:00+00:00"
        return httpx.Response(
            200,
            json=[
                dict(
                    candle_date_time_utc=at,
                    opening_price=100,
                    high_price=102,
                    low_price=98,
                    trade_price=101,
                    candle_acc_trade_volume=0.02,
                )
                for at in ["2026-09-01T00:02:00", "2026-09-01T00:00:00"]
            ],
        )

    async with httpx.AsyncClient(
        base_url="https://api.upbit.com", transport=httpx.MockTransport(responder)
    ) as client:
        data = await UpbitBroker(Settings(), client).candles("KRW-BTC", before="2026-09-01T00:03:00+00:00")
    assert len(data["candles"]) == 2
    assert data["nextBefore"] == "2026-09-01T00:00:00+00:00"
    assert Bar.parse(data["candles"][0]).volume == D(".02")


def test_fractional_grid_quantities_and_krw_minimum_order():
    spec = crypto_spec()
    qty = slot_quantity(spec, levels(spec)[0])
    assert 0 < qty < 1
    assert qty.as_tuple().exponent == -8
    with pytest.raises(ValueError, match="5,000"):
        crypto_spec(budget="10000")
    with pytest.raises(ValueError):
        StrategySpec(symbol="KRW-BTC", budget="1000", kind="trend")
    assert slot_quantity(StrategySpec(symbol="AAPL", kind="trend", budget="1000"), D(100)) == 1
    assert price_tick(D("1500999"), "upbit") == 1500000
    assert price_tick(D("15.09"), "upbit") == D("15.0")


def test_coin_backtest_can_fill_less_than_one_coin():
    spec = crypto_spec()
    start = datetime(2026, 9, 1, tzinfo=UTC)
    prices = [100000000, 94000000, 94500000, 101000000, 102000000]
    bars = [
        Bar(start + timedelta(minutes=i), D(p), D(p) + 2000000, D(p) - 2000000, D(p), D(2))
        for i, p in enumerate(prices)
    ]
    result = backtest(spec, bars, daily_loss=D(500000), drawdown=D(500000))
    assert result["currency"] == "KRW"
    assert result["trade_count"] > 0
    assert all(0 < D(t["quantity"]) < 1 for t in result["trades"])


def test_coin_dust_cannot_be_submitted_as_a_new_sell():
    spec = crypto_spec()
    state = initial_state(spec.budget)
    state["last_mid"] = "96000000"
    state["lots"] = {"0": {"quantity": "0.00000001", "cost": "1"}}
    decision, reason = decide(
        spec, state, Quote(spec.symbol, D(100000000), D(100000000), datetime.now(UTC)), []
    )
    assert decision is None
    assert "최소 주문" in reason


async def test_krw_risk_halt_does_not_halt_usd_portfolio(db):
    _, sessions = db
    settings = Settings(upbit_enabled=True)
    async with sessions.begin() as session:
        await bootstrap(session, settings)
    async with sessions.begin() as session:
        account = await session.get(AccountState, {"venue": "upbit", "mode": "paper"})
        account.daily_anchor = settings.capital_krw + settings.daily_loss_krw
        await check_risk(session, settings, {}, "", venue="upbit")
        await check_risk(session, settings, {}, "", venue="toss")
    async with sessions() as session:
        assert (await session.get(AccountState, {"venue": "upbit", "mode": "paper"})).halted
        assert not (await session.get(AccountState, {"venue": "toss", "mode": "paper"})).halted
        krw = await session.get(RuntimeState, "portfolio:upbit:paper")
        usd = await session.get(RuntimeState, "portfolio:toss:paper")
        assert krw.data["currency"] == "KRW" and usd.data["currency"] == "USD"
        assert krw.data["capital"] == "1000000.0000000000"


async def test_coin_live_strategy_is_rejected_even_when_stock_live_is_enabled(db):
    _, sessions = db
    async with sessions.begin() as session:
        with pytest.raises(ValueError, match="실제 주문"):
            await create_strategy(session, "coin", crypto_spec(), "live")


async def test_coin_paper_strategy_runs_while_stock_market_is_closed(db, monkeypatch):
    database, sessions = db
    settings = Settings(upbit_enabled=True)
    engine = Engine(settings)
    await engine.db.dispose()
    await engine.broker.close()
    await engine.upbit.close()
    engine.db, engine.sessions = database, sessions
    engine.broker = AsyncMock()
    engine.upbit = AsyncMock()
    engine.upbit.markets.return_value = {"KRW-BTC": {"market": "KRW-BTC"}}
    engine.upbit.orderbooks.return_value = [
        Quote("KRW-BTC", D(94000000), D(94000000), datetime.now(UTC), D(2), D(2))
    ]
    engine.upbit.candles.return_value = {"candles": []}
    async with sessions.begin() as session:
        await bootstrap(session, settings)
        strategy = await create_strategy(session, "coin paper", crypto_spec(), "paper")
        strategy.status = "RUNNING"
        strategy.state = {**strategy.state, "funded": True, "last_mid": "100000000"}
    await engine.cycle()
    async with sessions() as session:
        intent = await session.scalar(select(Intent))
        assert intent is not None and intent.venue == "upbit" and intent.quantity < 1
        assert intent.status == "PENDING"
        intent_id = intent.id
        next_tick = intent.updated_at.replace(tzinfo=UTC) + timedelta(seconds=1)

    class NextTickClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next_tick if tz else next_tick.replace(tzinfo=None)

    # Advance beyond the persisted timestamp; MySQL DATETIME has second precision.
    monkeypatch.setattr("cotrader.engine.datetime", NextTickClock)
    engine.quotes["KRW-BTC"] = Quote("KRW-BTC", D(94000000), D(94000000), next_tick, D(2), D(2))
    await engine.paper_fill(intent_id)
    async with sessions() as session:
        intent = await session.get(Intent, intent_id)
        assert intent.status == "FILLED"
        strategy = await session.get(Strategy, intent.strategy_id)
        assert D(strategy.state["quantity"]) > 0
    engine.broker.place.assert_not_called()
    engine.broker.orderbook.assert_not_called()


async def test_guide_is_public_but_crypto_account_is_private(db, tmp_path):
    _, sessions = db
    (tmp_path / "index.html").write_text('<div id="root"></div>')
    settings = Settings(
        auth_mode="github",
        public_url="https://example.com",
        github_client_id="fixture",
        github_client_secret="fixture",
        github_user_id=7,
        session_secret="x" * 32,
        static_dir=str(tmp_path),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings, sessions)), base_url="https://example.com"
    ) as client:
        assert (await client.get("/guide")).status_code == 200
        assert (await client.get("/api/account?venue=upbit")).status_code == 401
        assert (await client.get("/api/portfolio?venue=upbit")).status_code == 401
