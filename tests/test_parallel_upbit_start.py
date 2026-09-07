"""Starting a second coin must preserve verified resting orders on the first."""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from test_usdt_maker import chance, fixture, request, spec

from cotrader.broker import BrokerError
from cotrader.domain import D, Quote, funded_state, grid_quantity, levels
from cotrader.live import inventory_grid_spec
from cotrader.models import AccountState, Intent, Ledger, Strategy
from cotrader.services import approval_digest, create_strategy, enqueue, process_command, record_execution


async def setup_pair(db, partial=False):
    engine = await fixture(db)
    btc_chance = chance()
    btc_chance["market"]["id"] = "USDT-BTC"
    btc_chance["ask_account"].update(currency="BTC", balance=".01", avg_buy_price="50000")
    btc_quote = Quote("USDT-BTC", D(50000), D("50000.01"), datetime.now(UTC), D(1), D(1))
    btc_spec = inventory_grid_spec(request(symbol="USDT-BTC"), btc_chance, btc_quote)
    engine.upbit.chance.return_value = btc_chance
    engine.upbit.orderbooks.side_effect = None
    engine.upbit.orderbooks.return_value = [btc_quote]
    engine.upbit.markets.return_value["USDT-BTC"] = engine.upbit_markets["USDT-SOL"]
    details = {}
    async with engine.sessions.begin() as session:
        source = await create_strategy(session, "running SOL", spec(), "live")
        source.status, source.state = "RUNNING", {**funded_state(spec()), "funded": True}
        target = await create_strategy(session, "draft BTC", btc_spec, "live")
        account = await session.get(AccountState, {"venue": "upbit_usdt", "mode": "live"})
        account.capital = D(2000)
        for slot in range(spec().grids):
            intent = Intent(
                strategy_id=source.id,
                venue=source.venue,
                mode="live",
                symbol=source.symbol,
                side="SELL",
                quantity=grid_quantity(spec(), slot),
                price=levels(spec())[slot + 1],
                slot=slot,
                status="PENDING",
                broker_id=f"fixture-order-{slot}",
                costs_final=True,
            )
            session.add(intent)
            await session.flush()
            detail = {
                "orderId": intent.broker_id,
                "clientOrderId": intent.id,
                "symbol": intent.symbol,
                "side": intent.side,
                "quantity": str(intent.quantity),
                "price": str(intent.price),
                "status": "PENDING",
                "execution": {"filledQuantity": "0", "filledAmount": "0", "commission": "0", "tax": "0"},
            }
            if partial and slot == 0:
                detail["status"] = "PARTIAL_FILLED"
                detail["execution"].update(
                    filledQuantity=str(intent.quantity / 2),
                    filledAmount=str(intent.quantity / 2 * intent.price),
                    commission=".01",
                )
                await record_execution(session, intent, detail)
            details[intent.broker_id] = detail
    engine.upbit.orders.return_value = list(details.values())
    engine.upbit.order.side_effect = lambda order_id: details[order_id]
    return engine, source.id, target.id, details


async def books(session):
    strategies = (await session.scalars(select(Strategy).order_by(Strategy.id))).all()
    intents = (await session.scalars(select(Intent).order_by(Intent.id))).all()
    return (
        [(r.id, r.status, json.dumps(r.state, sort_keys=True)) for r in strategies],
        [(r.id, r.status, r.broker_id, r.filled_quantity, r.filled_amount, r.costs) for r in intents],
        await session.scalar(select(func.count()).select_from(Ledger)),
    )


@pytest.mark.parametrize("partial", [False, True])
async def test_btc_readiness_and_start_preserve_five_sol_orders_and_execution_book(db, partial):
    engine, source_id, target_id, _ = await setup_pair(db, partial)
    async with engine.sessions.begin() as session:
        target = await session.get(Strategy, target_id)
        before = await books(session)
        await engine.check_live_start(target, session)
        assert await books(session) == before
        command = await enqueue(
            session,
            "fixture-start",
            "start",
            {
                "strategy_id": target.id,
                "version": target.version,
                "approval": approval_digest(target, engine.settings),
            },
            "fixture",
        )
        await process_command(session, command, engine.settings, live_checked=True)
        assert command.status == "SUCCEEDED" and target.status == "RUNNING"
        source = await session.get(Strategy, source_id)
        assert source.status == "RUNNING"
        assert next(row for row in before[0] if row[0] == source_id)[2] == json.dumps(
            source.state, sort_keys=True
        )
        assert (await books(session))[1:] == before[1:]
    assert engine.upbit.order.await_count == 5
    assert engine.upbit.test_order.await_count == 5
    engine.upbit.place.assert_not_awaited()
    engine.upbit.cancel.assert_not_awaited()


@pytest.mark.parametrize(
    "obstruction",
    [
        "PREPARED",
        "SENDING",
        "UNKNOWN",
        "PENDING_CANCEL",
        "missing_broker_id",
        "estimated_costs",
        "paused_owner",
        "unfunded_owner",
        "missing_owner",
        "owner_symbol",
        "same_coin_cross_quote",
        "missing_open_order",
        "orderId",
        "clientOrderId",
        "symbol",
        "side",
        "quantity",
        "price",
        "status",
        "filledQuantity",
        "filledAmount",
        "commission",
        "missing_execution",
        "nonfinite",
        "lookup_error",
        "manual_target",
        "market_caution",
        "capital",
    ],
)
async def test_unresolved_mismatched_or_unsafe_orders_still_block_without_changing_books(db, obstruction):
    engine, source_id, target_id, details = await setup_pair(db)
    detail = details["fixture-order-0"]
    async with engine.sessions.begin() as session:
        source, target = await session.get(Strategy, source_id), await session.get(Strategy, target_id)
        intent = await session.scalar(select(Intent).where(Intent.broker_id == "fixture-order-0"))
        if obstruction in {"PREPARED", "SENDING", "UNKNOWN", "PENDING_CANCEL"}:
            intent.status = obstruction
        elif obstruction == "missing_broker_id":
            intent.broker_id = None
        elif obstruction == "estimated_costs":
            intent.costs_final = False
        elif obstruction == "paused_owner":
            source.status = "PAUSED"
        elif obstruction == "unfunded_owner":
            source.state = {**source.state, "funded": False}
        elif obstruction == "missing_owner":
            intent.strategy_id = "missing"
        elif obstruction == "owner_symbol":
            source.symbol = "USDT-ETH"
        elif obstruction == "same_coin_cross_quote":
            intent.venue, intent.symbol = "upbit", "KRW-BTC"
        elif obstruction == "missing_open_order":
            engine.upbit.orders.return_value = []
        elif obstruction in {"orderId", "clientOrderId", "symbol", "side", "status"}:
            detail[obstruction] = "unexpected"
        elif obstruction in {"quantity", "price"}:
            detail[obstruction] = str(D(detail[obstruction]) + 1)
        elif obstruction in {"filledQuantity", "filledAmount", "commission"}:
            detail["execution"][obstruction] = "1"
        elif obstruction == "missing_execution":
            del detail["execution"]["commission"]
        elif obstruction == "nonfinite":
            detail["execution"]["filledAmount"] = "NaN"
        elif obstruction == "lookup_error":
            engine.upbit.order.side_effect = BrokerError("fixture-unavailable")
        elif obstruction == "manual_target":
            engine.upbit.orders.return_value.append({"symbol": "USDT-BTC", "orderId": "manual"})
        elif obstruction == "market_caution":
            engine.upbit.markets.return_value["USDT-BTC"] = {
                "market_event": {"warning": False, "caution": {"PRICE_FLUCTUATIONS": True}}
            }
        elif obstruction == "capital":
            account = await session.get(AccountState, {"mode": "live", "venue": "upbit_usdt"})
            account.capital = D(500)
        await session.flush()
        before = await books(session)
        if obstruction == "capital":
            await engine.check_live_start(target, session)
            command = await enqueue(
                session,
                "fixture-start",
                "start",
                {
                    "strategy_id": target.id,
                    "version": target.version,
                    "approval": approval_digest(target, engine.settings),
                },
                "fixture",
            )
            with pytest.raises(ValueError, match="총예산"):
                await process_command(session, command, engine.settings, live_checked=True)
        else:
            with pytest.raises((ValueError, BrokerError)):
                await engine.check_live_start(target, session)
        assert await books(session) == before
    engine.upbit.place.assert_not_awaited()
    engine.upbit.cancel.assert_not_awaited()
