from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from cotrader.broker import BrokerError
from cotrader.config import Settings
from cotrader.domain import D, Quote, StrategySpec
from cotrader.engine import Engine
from cotrader.models import AccountState, Command, Intent, Strategy, now
from cotrader.services import approval_digest, bootstrap, create_strategy, enqueue, process_command


async def setup_engine(db):
    database, sessions = db
    settings = Settings(
        auth_mode="telegram",
        public_url="https://cotrader.example",
        market_source="toss",
        live_enabled=True,
        TELEGRAM_API_KEY="test-bot-token",
        TELEGRAM_ME=7,
        session_secret="x" * 32,
        account_seq=1,
    )
    engine = Engine(settings)
    await engine.db.dispose()
    await engine.broker.close()
    engine.db, engine.sessions, engine.lock = database, sessions, AsyncMock()
    engine.broker = AsyncMock()
    engine.broker.buying_power.return_value = D(5000)
    engine.broker.sellable.return_value = D(10)
    engine.quotes["TEST"] = Quote("TEST", D(94), D(94), datetime.now(UTC), D(1000), D(1000))
    engine.calendar = {
        "today": {
            "date": "2026-09-01",
            "regularMarket": {
                "startTime": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                "endTime": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        }
    }
    async with sessions.begin() as session:
        await bootstrap(session, settings)
        strategy = await create_strategy(
            session,
            "주문 복구 검증",
            StrategySpec(symbol="TEST", budget="1000", lower="90", upper="110", grids=4),
            "live",
        )
        command = await enqueue(
            session,
            "start-live",
            "start",
            {"strategy_id": strategy.id, "version": 1, "approval": approval_digest(strategy, settings)},
            "telegram:7",
        )
        await process_command(session, command, settings)
        intent = Intent(
            strategy_id=strategy.id,
            symbol="TEST",
            mode="live",
            side="BUY",
            quantity=D(2),
            price=D(95),
            slot=1,
        )
        session.add(intent)
        await session.flush()
        return engine, strategy.id, intent.id


async def test_unknown_submission_reuses_same_key_only_inside_safe_window(db):
    engine, _, intent_id = await setup_engine(db)
    engine.broker.place.side_effect = BrokerError("fixture-timeout", ambiguous=True)
    await engine.submit_live(intent_id)
    async with engine.sessions() as session:
        intent = await session.get(Intent, intent_id)
        assert intent.status == "UNKNOWN"
        assert intent.submitted_at is not None
    await engine.reconcile_orders()
    assert engine.broker.place.await_count == 2
    assert all(call.args[0].id == intent_id for call in engine.broker.place.await_args_list)
    async with engine.sessions.begin() as session:
        intent = await session.get(Intent, intent_id)
        intent.submitted_at = now() - timedelta(minutes=10)
    await engine.reconcile_orders()
    assert engine.broker.place.await_count == 2
    async with engine.sessions() as session:
        assert (await session.get(Intent, intent_id)).status == "UNKNOWN"


async def test_unknown_order_is_not_resubmitted_after_pause(db):
    engine, strategy_id, intent_id = await setup_engine(db)
    async with engine.sessions.begin() as session:
        strategy = await session.get(Strategy, strategy_id)
        strategy.status = "PAUSED"
        intent = await session.get(Intent, intent_id)
        intent.status, intent.submitted_at = "SENDING", now()
    await engine.reconcile_orders()
    assert engine.broker.place.await_count == 0


async def test_unknown_result_blocks_new_orders_for_other_symbols(db):
    engine, strategy_id, intent_id = await setup_engine(db)
    async with engine.sessions.begin() as session:
        first = await session.get(Intent, intent_id)
        first.status = "UNKNOWN"
        session.add(
            Intent(
                strategy_id=strategy_id, symbol="OTHER", mode="live", side="BUY", quantity=D(1), price=D(10)
            )
        )
    await engine.dispatch()
    assert engine.broker.place.await_count == 0


async def test_late_fill_is_reconciled_even_after_risk_halt(db):
    engine, strategy_id, intent_id = await setup_engine(db)
    async with engine.sessions.begin() as session:
        intent = await session.get(Intent, intent_id)
        intent.status, intent.broker_id = "PENDING_CANCEL", "fixture-order"
        (await session.get(AccountState, {"venue": "toss", "mode": "live"})).halted = True
        (await session.get(Strategy, strategy_id)).status = "PAUSED"
    engine.broker.order.return_value = {
        "symbol": "TEST",
        "side": "BUY",
        "status": "FILLED",
        "execution": {"filledQuantity": "2", "filledAmount": "190", "commission": "0.19", "tax": "0"},
    }
    await engine.reconcile_orders()
    async with engine.sessions() as session:
        assert (await session.get(Intent, intent_id)).status == "FILLED"
        assert D((await session.get(Strategy, strategy_id)).state["quantity"]) == 2
    assert engine.broker.place.await_count == 0


async def test_cancel_does_not_liquidate_holdings(db):
    engine, strategy_id, intent_id = await setup_engine(db)
    async with engine.sessions.begin() as session:
        (await session.get(Strategy, strategy_id)).status = "PAUSED"
        intent = await session.get(Intent, intent_id)
        intent.status, intent.broker_id = "PENDING", "fixture-order"
    await engine.cancel_stopped()
    engine.broker.cancel.assert_awaited_once_with("fixture-order")
    engine.broker.place.assert_not_awaited()


async def test_quote_expiring_during_buying_power_query_prevents_submission(db):
    engine, _, intent_id = await setup_engine(db)

    async def delayed_balance():
        engine.quotes["TEST"] = Quote("TEST", D(94), D(94), datetime.now(UTC) - timedelta(minutes=1))
        return D(5000)

    engine.broker.buying_power.side_effect = delayed_balance
    await engine.submit_live(intent_id)
    engine.broker.place.assert_not_awaited()
    async with engine.sessions() as session:
        assert (await session.get(Intent, intent_id)).status == "PREPARED"


async def test_missing_portfolio_valuation_blocks_prepared_order(db):
    engine, _, _ = await setup_engine(db)
    await engine.dispatch()
    engine.broker.place.assert_not_awaited()


async def test_market_data_failure_still_reconciles_and_cancels_orders(db):
    engine, _, _ = await setup_engine(db)
    engine.market_refresh = AsyncMock(side_effect=BrokerError("market-data-unavailable"))
    engine.resolve_commands = AsyncMock()
    engine.reconcile_orders = AsyncMock()
    engine.verify_holdings = AsyncMock()
    engine.cancel_stopped = AsyncMock()
    engine.evaluate = AsyncMock()
    engine.dispatch = AsyncMock()
    engine.ingest_page = AsyncMock()
    await engine.cycle()
    engine.reconcile_orders.assert_awaited_once()
    engine.cancel_stopped.assert_awaited_once()
    assert engine.quotes == {}


@pytest.mark.parametrize("already_linked", [False, True])
async def test_manual_order_resolution_rejects_an_already_linked_broker_id(db, already_linked):
    engine, strategy_id, intent_id = await setup_engine(db)
    engine.broker.order.return_value = {
        "orderId": "confirmed",
        "symbol": "TEST",
        "side": "BUY",
        "quantity": "2",
        "price": "95",
        "status": "PENDING",
        "execution": {"filledQuantity": "0", "filledAmount": "0", "commission": "0", "tax": "0"},
    }
    async with engine.sessions.begin() as session:
        (await session.get(Intent, intent_id)).status = "UNKNOWN"
        session.add(
            Command(
                id="manual-resolution",
                action="resolve",
                actor="local",
                status="RUNNING",
                payload={"intent_id": intent_id, "broker_id": "confirmed"},
            )
        )
        if already_linked:
            session.add(
                Intent(
                    strategy_id=strategy_id,
                    symbol="TEST",
                    mode="live",
                    side="BUY",
                    quantity=D(2),
                    price=D(95),
                    broker_id="confirmed",
                    status="CANCELED",
                )
            )
    await engine.resolve_commands()
    async with engine.sessions() as session:
        command = await session.get(Command, "manual-resolution")
        intent = await session.get(Intent, intent_id)
        assert command.status == ("REJECTED" if already_linked else "SUCCEEDED")
        assert intent.status == ("UNKNOWN" if already_linked else "PENDING")
    engine.broker.place.assert_not_awaited()
