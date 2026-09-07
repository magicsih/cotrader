import asyncio
import copy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from cotrader.config import Settings
from cotrader.domain import ETF_UNIVERSE, D, Quote, StrategySpec, initial_state
from cotrader.engine import Engine
from cotrader.etf_data import calendar, last_completed_session, parse_history
from cotrader.models import Intent, RuntimeState, Strategy
from cotrader.rotation import (
    apply_execution,
    decide_rotation,
    held,
    monthly_targets,
    signal_date,
    specification,
)
from cotrader.services import (
    approval_digest,
    bootstrap,
    check_risk,
    create_strategy,
    enqueue,
    process_command,
    record_execution,
)

AT = datetime(2026, 9, 8, 14, tzinfo=UTC)


@pytest.fixture
def history():
    dates = [s.date().isoformat() for s in calendar(2026).sessions_in_range("2025-01-02", "2026-09-04")]
    return {
        symbol: [
            {"date": day, "close": "100", "total_return": str(D(1) + D(index) * rate), "dividend": "0"}
            for index, day in enumerate(dates)
        ]
        for symbol, rate in zip(
            ETF_UNIVERSE, map(D, ["0.001", "0.003", "0.002", "0", "0", "0.004", "0"]), strict=True
        )
    }


def quotes(at=AT):
    return {symbol: Quote(symbol, D(100), D(100), at, D(100), D(100)) for symbol in ETF_UNIVERSE}


def document(symbol, history):
    rows = history[symbol]
    return {
        "chart": {
            "error": None,
            "result": [
                {
                    "meta": {"symbol": symbol, "currency": "USD", "instrumentType": "ETF"},
                    "timestamp": [
                        int(datetime.fromisoformat(r["date"] + "T14:00:00+00:00").timestamp()) for r in rows
                    ],
                    "indicators": {
                        "quote": [{"close": [float(r["close"]) for r in rows], "volume": [1000] * len(rows)}]
                    },
                    "events": {},
                }
            ],
        }
    }


def test_policy_is_toss_only_and_bound_to_approval():
    spec = specification()
    assert spec.symbols == ETF_UNIVERSE
    assert spec.rotation_policy == "monthly_252_top2_v1"
    with pytest.raises(ValueError):
        StrategySpec(**{**spec.model_dump(), "venue": "upbit", "symbol": "KRW-BTC"})
    with pytest.raises(ValueError):
        StrategySpec(**{**spec.model_dump(), "symbol": "QQQ"})
    assert quotes()["QQQ"].valid(spec, AT)
    assert not Quote("AAPL", D(10), D(10), AT).valid(spec, AT)


def test_top_two_and_negative_slots_remain_cash(history):
    weights, _ = monthly_targets(history, "2026-09-04")
    assert weights == {"GLD": "0.5", "QQQ": "0.5"}
    for symbol in ETF_UNIVERSE:
        if symbol != "GLD":
            for row in history[symbol]:
                row["total_return"] = "1"
    assert monthly_targets(history, "2026-09-04")[0] == {"GLD": "0.5"}
    for row in history["GLD"]:
        row["total_return"] = "1"
    assert monthly_targets(history, "2026-09-04")[0] == {}


def test_no_future_rows_or_silent_universe_changes(history):
    expected = monthly_targets(history, "2026-09-01")
    history["QQQ"].append({"date": "2027-01-01", "total_return": "999999"})
    assert monthly_targets(history, "2026-09-01") == expected
    history["IWM"].pop(50)
    with pytest.raises(ValueError, match="거래일"):
        monthly_targets(history, "2026-09-01")
    del history["IWM"]
    with pytest.raises(ValueError, match="7개"):
        monthly_targets(history, "2026-09-01")


def test_initial_entry_and_month_lock_survive_restart(history):
    assert signal_date(history, "2026-09-04", {}) == "2026-09-04"
    assert signal_date(history, "2026-09-04", {"rotation_month": "2026-09"}) is None
    assert signal_date(history, "2026-09-04", {"rotation_month": "2026-08"}) == "2026-09-01"


def test_sells_first_and_never_spends_unfilled_proceeds(history):
    state = initial_state(D(5000))
    apply_execution(state, "SPY", "BUY", D(40), D(4000), D(4))
    decision, _ = decide_rotation(specification(), state, quotes(), history, AT, "2026-09-04")
    assert (decision.symbol, decision.side, decision.quantity) == ("SPY", "SELL", D(40))
    assert D(state["cash"]) == 996
    apply_execution(state, "SPY", "SELL", D(20), D(2000), D(2))
    decision, _ = decide_rotation(specification(), state, quotes(), history, AT, "2026-09-04")
    assert decision.side == "SELL" and decision.quantity == 20
    apply_execution(state, "SPY", "SELL", D(20), D(2000), D(2))
    decision, _ = decide_rotation(specification(), state, quotes(), history, AT, "2026-09-04")
    assert decision.side == "BUY" and decision.symbol == "QQQ"
    assert decision.quantity * decision.price * D("1.001") <= D(state["cash"])


def test_per_asset_basis_and_late_fee_updates():
    state = initial_state(D(5000))
    apply_execution(state, "QQQ", "BUY", D(2), D(1000), D(1))
    apply_execution(state, "GLD", "BUY", D(3), D(900), D("0.9"))
    apply_execution(state, "QQQ", "SELL", D(1), D(550), D("0.55"))
    apply_execution(state, "QQQ", "SELL", D(0), D(0), D("0.05"))
    assert held(state, "QQQ") == 1 and held(state, "GLD") == 3
    assert D(state["cost_basis"]) == 1400
    assert D(state["realized"]) == 50
    assert D(state["cash"]) == D("3647.5")
    assert D(state["costs"]) == D("2.5")
    with pytest.raises(ValueError):
        apply_execution(state, "SPY", "SELL", D(1), D(100), D(0))


def test_missing_or_stale_quotes_and_incomplete_day_block_decisions(history):
    spec, state = specification(), initial_state(D(5000))
    stale = quotes(AT - timedelta(minutes=1))
    assert decide_rotation(spec, state, stale, history, AT, "2026-09-04")[0] is None
    assert decide_rotation(spec, state, quotes(), history, AT, "2026-09-07")[0] is None


def test_daily_goal_does_not_churn_with_intraday_price_changes(history):
    state = initial_state(D(5000))
    first, _ = decide_rotation(specification(), state, quotes(), history, AT, "2026-09-04")
    goals = copy.deepcopy(state["goals"])
    apply_execution(state, first.symbol, "BUY", first.quantity, first.quantity * first.price, D("2.5"))
    changed = quotes()
    changed["QQQ"] = Quote("QQQ", D(150), D(150), AT)
    decide_rotation(specification(), state, changed, history, AT, "2026-09-04")
    assert state["goals"] == goals


def test_calendar_handles_holiday_and_early_close():
    assert last_completed_session(datetime(2026, 9, 7, 20, tzinfo=UTC)) == "2026-09-04"
    assert last_completed_session(datetime(2026, 11, 27, 18, 4, tzinfo=UTC)) == "2026-11-25"
    assert last_completed_session(datetime(2026, 11, 27, 18, 5, tzinfo=UTC)) == "2026-11-27"


def test_daily_parser_validates_dates_and_builds_causal_dividend_index(history):
    body = document("QQQ", history)
    result = body["chart"]["result"][0]
    timestamp = result["timestamp"][-1]
    result["events"] = {"dividends": {str(timestamp): {"date": timestamp, "amount": 1}}}
    rows = parse_history("QQQ", body, AT)
    assert D(rows[-1]["total_return"]) == D("1.01")
    result["timestamp"].pop(10)
    result["indicators"]["quote"][0]["close"].pop(10)
    result["indicators"]["quote"][0]["volume"].pop(10)
    with pytest.raises(ValueError, match="거래일"):
        parse_history("QQQ", body, AT)


@pytest.mark.parametrize("fault", ["wrong_symbol", "zero_volume", "missing_close", "split", "duplicate"])
def test_daily_parser_fails_closed(history, fault):
    body = document("QQQ", history)
    result = body["chart"]["result"][0]
    if fault == "wrong_symbol":
        result["meta"]["symbol"] = "SPY"
    elif fault == "zero_volume":
        result["indicators"]["quote"][0]["volume"][-1] = 0
    elif fault == "missing_close":
        result["indicators"]["quote"][0]["close"][-1] = None
    elif fault == "split":
        result["events"] = {"splits": {"split": {"date": result["timestamp"][-1]}}}
    else:
        result["timestamp"][-1] = result["timestamp"][-2]
    with pytest.raises((ValueError, ArithmeticError)):
        parse_history("QQQ", body, AT)


async def test_funded_qqq_and_rotation_cannot_overlap(db):
    _, sessions = db
    settings = Settings(capital_usd="5000")
    async with sessions.begin() as session:
        await bootstrap(session, settings)
        existing = await create_strategy(
            session, "QQQ", StrategySpec(symbol="QQQ", kind="trend", budget="1000"), "paper"
        )
        existing.state = {**existing.state, "funded": True}
        rotation = await create_strategy(session, "ETF 교체", specification("3000"), "paper")
        command = await enqueue(
            session,
            "overlap",
            "start",
            {
                "strategy_id": rotation.id,
                "version": rotation.version,
                "approval": approval_digest(rotation, settings),
            },
            "local",
        )
        with pytest.raises(ValueError, match="같은 종목"):
            await process_command(session, command, settings, live_checked=True)


async def test_execution_reconciliation_and_portfolio_valuation(db):
    _, sessions = db
    settings = Settings(daily_loss_usd="150", drawdown_usd="750")
    async with sessions.begin() as session:
        await bootstrap(session, settings)
        row = await create_strategy(session, "ETF 교체", specification(), "paper")
        row.state = {**row.state, "funded": True}
        row.status = "RUNNING"
        for symbol, quantity in [("QQQ", 2), ("GLD", 3)]:
            intent = Intent(
                strategy_id=row.id,
                venue="toss",
                mode="paper",
                symbol=symbol,
                side="BUY",
                quantity=D(quantity),
                price=D(100),
            )
            session.add(intent)
            await session.flush()
            order = {
                "symbol": symbol,
                "side": "BUY",
                "status": "FILLED",
                "execution": {
                    "filledQuantity": str(quantity),
                    "filledAmount": str(quantity * 100),
                    "commission": str(D(quantity) / 10),
                    "tax": "0",
                },
            }
            await record_execution(session, intent, order)
            await record_execution(session, intent, order)
        assert D(row.state["cash"]) == D("4499.5")
        assert held(row.state, "QQQ") == 2 and held(row.state, "GLD") == 3
        await check_risk(session, settings, quotes(datetime.now(UTC)), "2026-09-08")
        await session.flush()
        report = await session.get(RuntimeState, "portfolio:toss:paper")
        assert report.data["complete"] and D(report.data["equity"]) == D("4999.5")
        missing = quotes(datetime.now(UTC))
        del missing["GLD"]
        await check_risk(session, settings, missing, "2026-09-08")
        assert report.data["equity"] is None


async def engine_with_rotation(db, history):
    engine = Engine(Settings())
    await engine.db.dispose()
    engine.db, engine.sessions = db
    engine.quotes = quotes()
    engine.stocks = {
        s: {"currency": "USD", "status": "ACTIVE", "securityType": "ETF", "leverageFactor": "1"}
        for s in ETF_UNIVERSE
    }
    engine.calendar = {
        "today": {
            "date": "2026-09-08",
            "regularMarket": {
                "startTime": "2026-09-08T13:30:00+00:00",
                "endTime": "2026-09-08T20:00:00+00:00",
            },
        }
    }
    engine.rotation_history = {
        "series": history,
        "last_session": "2026-09-04",
        "checked_at": AT.isoformat(),
        "fingerprint": "fixture",
    }
    async with engine.sessions.begin() as session:
        await bootstrap(session, engine.settings)
        row = await create_strategy(session, "ETF 교체", specification(), "paper")
        row.status, row.state = "RUNNING", {**row.state, "funded": True}
        row_id = row.id
    return engine, row_id


async def test_pending_order_blocks_second_leg_and_rejection_pauses(db, history):
    engine, row_id = await engine_with_rotation(db, history)
    try:
        async with engine.sessions.begin() as session:
            row = await session.get(Strategy, row_id)
            await engine.evaluate_rotation(session, row, specification(), AT)
            await session.flush()
            intent = await session.scalar(select(Intent))
            assert intent and intent.symbol == "QQQ"
            await engine.evaluate_rotation(session, row, specification(), AT)
            assert len((await session.scalars(select(Intent))).all()) == 1
            intent.status, intent.created_at = "REJECTED", AT.replace(tzinfo=None)
            await engine.evaluate_rotation(session, row, specification(), AT)
            assert row.status == "PAUSED"
    finally:
        await engine.broker.close()
        await engine.upbit.close()


async def test_stale_daily_source_or_extended_session_cannot_create_order(db, history):
    engine, row_id = await engine_with_rotation(db, history)
    try:
        async with engine.sessions.begin() as session:
            row = await session.get(Strategy, row_id)
            await engine.evaluate_rotation(session, row, specification(), AT - timedelta(hours=2))
            assert await session.scalar(select(Intent.id)) is None
            engine.rotation_history["checked_at"] = (AT - timedelta(hours=2)).isoformat()
            await engine.evaluate_rotation(session, row, specification(), AT)
            assert await session.scalar(select(Intent.id)) is None
    finally:
        await engine.broker.close()
        await engine.upbit.close()


async def test_holdings_reconciliation_checks_every_etf_not_unrelated_holdings(db, history):
    engine, row_id = await engine_with_rotation(db, history)
    try:
        async with engine.sessions.begin() as session:
            row = await session.get(Strategy, row_id)
            row.mode = "live"
        engine.broker.holdings = AsyncMock(return_value={"items": [{"symbol": "AAPL", "quantity": "100"}]})
        engine.broker.orders = AsyncMock(return_value=[])
        await engine.verify_holdings()
        async with engine.sessions() as session:
            assert (await session.get(Strategy, row_id)).status == "RUNNING"
        engine.broker.holdings.return_value = {"items": [{"symbol": "GLD", "quantity": "1"}]}
        await engine.verify_holdings()
        async with engine.sessions() as session:
            assert (await session.get(Strategy, row_id)).status == "PAUSED"
    finally:
        await engine.broker.close()
        await engine.upbit.close()


async def test_public_data_download_does_not_block_engine_and_failure_is_visible(db, history, monkeypatch):
    engine, _ = await engine_with_rotation(db, history)
    waiting = asyncio.Event()

    async def delayed(_at):
        await waiting.wait()
        raise ValueError("GLD 거래일 누락")

    monkeypatch.setattr("cotrader.etf_data.fetch_history", delayed)
    try:
        await asyncio.wait_for(engine.refresh_rotation_history(AT), timeout=0.5)
        assert engine.rotation_task and not engine.rotation_task.done()
        waiting.set()
        with pytest.raises(ValueError):
            await engine.rotation_task
        await engine.refresh_rotation_history(AT)
        assert engine.rotation_task is None
        assert "GLD 거래일 누락" in engine.rotation_error
    finally:
        await engine.broker.close()
        await engine.upbit.close()


async def test_start_readback_checks_all_assets_cash_and_fees_without_orders(db, history, monkeypatch):
    engine, row_id = await engine_with_rotation(db, history)
    engine.refresh_rotation_history = AsyncMock()
    engine.broker.place = AsyncMock()
    engine.broker.account_snapshot = AsyncMock(
        return_value={
            "us_commission_rate": "0.001",
            "cash_buying_power": {"USD": "5500"},
            "holdings": {"items": []},
        }
    )
    engine.broker.stocks = AsyncMock(return_value=[{"symbol": s, **v} for s, v in engine.stocks.items()])
    engine.broker.orders = AsyncMock(return_value=[])
    monkeypatch.setattr("cotrader.etf_data.last_completed_session", lambda _at: "2026-09-04")
    try:
        async with engine.sessions() as session:
            row = await session.get(Strategy, row_id)
            await engine.check_rotation_start(row, specification(), set())
            engine.broker.account_snapshot.return_value["holdings"]["items"] = [
                {"symbol": "GLD", "quantity": "1"}
            ]
            with pytest.raises(ValueError, match="기존 보유분"):
                await engine.check_rotation_start(row, specification(), set())
            engine.broker.account_snapshot.return_value["holdings"]["items"] = []
            engine.broker.account_snapshot.return_value["cash_buying_power"]["USD"] = "4000"
            with pytest.raises(ValueError, match="공동 예산"):
                await engine.check_rotation_start(row, specification(), set())
            engine.broker.account_snapshot.return_value["cash_buying_power"]["USD"] = "5500"
            engine.broker.account_snapshot.return_value["us_commission_rate"] = "0.002"
            with pytest.raises(ValueError, match="수수료"):
                await engine.check_rotation_start(row, specification(), set())
            engine.broker.place.assert_not_awaited()
    finally:
        await engine.broker.close()
        await engine.upbit.close()


async def test_regular_close_guard_applies_before_preparing_orders(db, history):
    engine, _ = await engine_with_rotation(db, history)
    try:
        assert engine.rotation_submission_open(AT)
        assert not engine.rotation_submission_open(AT.replace(hour=19, minute=59))
        assert not engine.rotation_submission_open(AT.replace(hour=20))
    finally:
        await engine.broker.close()
        await engine.upbit.close()


async def test_rotation_does_not_collect_unused_toss_minute_bars(db, history):
    engine, _ = await engine_with_rotation(db, history)
    engine.broker.calendar = AsyncMock(return_value=engine.calendar)
    stock_rows = {s: {"symbol": s, **v} for s, v in engine.stocks.items()}
    engine.broker.stocks = AsyncMock(side_effect=lambda symbols: [stock_rows[s] for s in symbols])
    engine.broker.orderbook = AsyncMock(side_effect=lambda symbol: quotes()[symbol])
    engine.broker.candles = AsyncMock()
    engine.broker.stream = AsyncMock()
    try:
        await engine.market_refresh(list(ETF_UNIVERSE), AT, candle_symbols=set())
        engine.broker.candles.assert_not_awaited()
        assert len(engine.stocks) == 7
    finally:
        if engine.stream_task:
            await engine.stream_task
        await engine.broker.close()
        await engine.upbit.close()


async def test_live_sell_is_limited_by_each_etf_not_total_share_count(db):
    from test_engine import setup_engine

    from cotrader.broker import BrokerError

    engine, row_id, intent_id = await setup_engine(db)
    try:
        async with engine.sessions.begin() as session:
            row = await session.get(Strategy, row_id)
            row.config, row.symbol = specification().model_dump(mode="json"), "ETF-ROTATION"
            state = {**initial_state(D(5000)), "funded": True}
            apply_execution(state, "QQQ", "BUY", D(1), D(100), D("0.1"))
            apply_execution(state, "GLD", "BUY", D(5), D(500), D("0.5"))
            row.state = state
            intent = await session.get(Intent, intent_id)
            intent.symbol, intent.side, intent.slot = "QQQ", "SELL", None
        engine.quotes["QQQ"] = Quote("QQQ", D(94), D(94), datetime.now(UTC), D(1000), D(1000))
        with pytest.raises(BrokerError, match="sellable-quantity-mismatch"):
            await engine.submit_live(intent_id)
        engine.broker.place.assert_not_awaited()
    finally:
        await engine.upbit.close()


async def test_api_creates_reviewable_rotation_draft_without_orders(db):
    from cotrader.api import create_app
    from cotrader.models import Command
    from cotrader.telegram_views import strategy as telegram_strategy

    _, sessions = db
    app = create_app(Settings(), sessions_override=sessions)
    body = {
        "name": "ETF 월간 모멘텀 252 · 상위 2개",
        "mode": "live",
        "spec": specification().model_dump(mode="json"),
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)), base_url="http://127.0.0.1:8000"
    ) as client:
        preview = await client.post("/api/strategies/preview", json=body)
        assert preview.status_code == 200
        assert preview.json()["rotation"]["top"] == 2 and preview.json()["sessions"] == "미국 정규장"
        result = await client.post("/api/strategies", json=body)
        assert result.status_code == 201
        saved = result.json()
        assert saved["status"] == "DRAFT" and saved["state"]["funded"] is False
        async with sessions() as session:
            assert await session.scalar(select(Intent.id)) is None
            assert await session.scalar(select(Command.id)) is None
            row = await session.get(Strategy, saved["id"])
            blocks, buttons = telegram_strategy(row, Settings())
            assert "ETF 월간 교체" in str(blocks)
            assert "EMA" not in str(blocks)
            assert any(b.get("callback_data", "").startswith("run:") for line in buttons for b in line)
