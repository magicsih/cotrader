import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from cotrader.config import Settings
from cotrader.domain import D, Quote, StrategySpec, apply_fill, decide, funded_state, grid_quantity, levels
from cotrader.engine import Engine
from cotrader.live import InventoryGridRequest, inventory_grid_spec
from cotrader.models import Intent, Strategy
from cotrader.research import backtest
from cotrader.services import (
    approval_digest,
    bootstrap,
    check_risk,
    create_strategy,
    enqueue,
    process_command,
    record_execution,
)


def chance(balance="6.00000001", locked="0", cash="0", average="105000"):
    return dict(
        bid_fee="0.0005",
        ask_fee="0.0005",
        maker_bid_fee="0.0005",
        maker_ask_fee="0.0005",
        market=dict(
            id="KRW-SOL",
            state="active",
            order_sides=["bid", "ask"],
            bid_types=["limit"],
            ask_types=["limit"],
            bid={"min_total": "5000"},
            ask={"min_total": "5000"},
            max_total="1000000000",
        ),
        bid_account=dict(currency="KRW", balance=cash, locked="0"),
        ask_account=dict(currency="SOL", balance=balance, locked=locked, avg_buy_price=average),
    )


def quote(price="100000"):
    return Quote("KRW-SOL", D(price), D(price), datetime.now(UTC), D(100), D(100))


def request(**changes):
    return InventoryGridRequest.model_validate(
        dict(symbol="KRW-SOL", allocation="all", first_sell_basis="average", **changes)
    )


def spec():
    return inventory_grid_spec(request(), chance(), quote())


def fill(state, decision, fraction="1"):
    size = decision.quantity * D(fraction)
    amount = decision.price * size
    apply_fill(state, decision.side, size, amount, amount * D("0.0005"), decision.slot)


def test_inventory_is_distributed_without_losing_remainder_or_inventing_cash():
    s = spec()
    state = funded_state(s)
    assert sum((grid_quantity(s, i) for i in range(s.grids)), D(0)) == s.inventory_quantity
    assert sum((D(lot["cost"]) for lot in state["lots"].values()), D(0)) == s.budget
    assert state["cash"] == "0" and state["realized"] == "0"
    assert D(state["cost_basis"]) != s.inventory_average_price * s.inventory_quantity
    assert levels(s)[1] * (1 - s.commission_rate) >= s.inventory_average_price * (1 + s.commission_rate)
    assert decide(s, state, quote(), []) == (None, "보유 코인 매도·재매수 가격 대기")


def test_sell_buy_sell_cycles_keep_coin_target_and_accumulate_net_cash():
    s = spec()
    state = funded_state(s)
    prices = levels(s)
    for _ in range(4):
        sell = decide(s, state, quote(str(prices[1])), [])[0]
        assert sell.side == "SELL" and sell.slot == 0 and sell.quantity == grid_quantity(s, 0)
        fill(state, sell)
        buy = decide(s, state, quote(str(prices[0])), [])[0]
        assert buy.side == "BUY" and buy.slot == 0 and buy.quantity == sell.quantity
        fill(state, buy)
        assert D(state["quantity"]) == s.inventory_quantity
    profit = grid_quantity(s, 0) * (prices[1] * D("0.9995") - prices[0] * D("1.0005")) * 4
    assert D(state["cash"]) == profit > 0
    assert sum((D(lot["cash"]) for lot in state["lots"].values()), D(0)) == profit


def test_partial_sell_then_partial_buy_survives_json_restart_and_never_spends_other_slot():
    s = spec()
    state = funded_state(s)
    prices = levels(s)
    sell = decide(s, state, quote(str(prices[1])), [])[0]
    fill(state, sell, "0.5")
    state = json.loads(json.dumps(state))
    buy = decide(s, state, quote(str(prices[0])), [])[0]
    assert buy.quantity == sell.quantity / 2
    fill(state, buy, "0.5")
    resumed = decide(s, json.loads(json.dumps(state)), quote(str(prices[0])), [])[0]
    assert resumed.side == "BUY" and resumed.quantity == sell.quantity / 4
    fill(state, resumed)
    assert D(state["quantity"]) == s.inventory_quantity
    assert all(lot["cash"] == "0" for i, lot in state["lots"].items() if i != "0")
    # Unrelated strategy cash cannot fund a slot that has never sold.
    state["cash"] = "10000000"
    state["lots"]["1"]["quantity"] = "0"
    assert decide(s, state, quote(str(prices[0])), [])[0] is None


def test_rebuy_is_bounded_by_its_slot_net_proceeds():
    s = spec()
    state = funded_state(s)
    state["lots"]["0"].update(quantity="0", cash="10000")
    buy = decide(s, state, quote(str(levels(s)[0])), [])[0]
    assert buy.quantity * buy.price * (1 + s.commission_rate) <= 10000
    before = json.dumps(state, sort_keys=True)
    with pytest.raises(ValueError, match="매도대금"):
        apply_fill(state, "BUY", D("1"), D("10001"), D("5"), 0)
    assert json.dumps(state, sort_keys=True) == before


@pytest.mark.parametrize(
    "changes", [dict(locked="1"), dict(balance="0"), dict(balance="1.000000001"), dict(average="0")]
)
def test_draft_refuses_locked_empty_invalid_precision_and_missing_average(changes):
    with pytest.raises(ValueError):
        inventory_grid_spec(request(), chance(**changes), quote())


def test_current_price_basis_can_start_below_historical_average_and_rounds_up():
    r = InventoryGridRequest(symbol="KRW-SOL", allocation="all", first_sell_basis="market")
    s = inventory_grid_spec(r, chance(average="200000"), quote("100100"))
    assert levels(s)[1] >= D("100100") * D("1.02")
    assert levels(s)[1] < s.inventory_average_price


def test_inventory_spec_cannot_fabricate_budget_or_be_used_as_historical_cash_backtest():
    s = spec()
    with pytest.raises(ValueError, match="일치"):
        StrategySpec.model_validate({**s.model_dump(), "budget": s.budget + 1})
    with pytest.raises(ValueError, match="과거 검증"):
        backtest(s, [])


async def engine_fixture(db):
    settings = Settings(
        runtime_role="engine", auth_mode="github", upbit_enabled=True, upbit_live_enabled=True
    )
    engine = Engine(settings)
    await engine.db.dispose()
    await engine.broker.close()
    await engine.upbit.close()
    engine.db, engine.sessions, engine.lock = *db, AsyncMock()
    engine.upbit, engine.broker = AsyncMock(), AsyncMock()
    engine.upbit.chance.return_value = chance()
    engine.upbit.orders.return_value = []
    engine.upbit.orderbooks.return_value = [quote()]
    engine.upbit.markets.return_value = {
        "KRW-SOL": {"market_event": {"warning": False, "caution": {"PRICE_FLUCTUATIONS": False}}}
    }
    async with engine.sessions.begin() as session:
        await bootstrap(session, settings)
    return engine


async def start(session, row, engine, command_id):
    await engine.check_live_start(row)
    command = await enqueue(
        session,
        command_id,
        "start",
        dict(strategy_id=row.id, version=row.version, approval=approval_digest(row, engine.settings)),
        "fixture",
    )
    await process_command(session, command, engine.settings, live_checked=True)


async def test_prepare_is_unfunded_tests_every_sell_and_start_imports_once(db):
    engine = await engine_fixture(db)
    async with engine.sessions.begin() as session:
        row = await engine.prepare_inventory(session, request().model_dump(mode="json"))
        assert row.status == "DRAFT" and not row.state["funded"]
        assert row.state["quantity"] == row.state["cash"] == "0"
        assert await session.scalar(select(func.count()).select_from(Intent)) == 0
    assert engine.upbit.test_order.await_count == 5
    assert all(call.args[0].side == "SELL" for call in engine.upbit.test_order.await_args_list)
    async with engine.sessions.begin() as session:
        row = await session.get(Strategy, row.id)
        await start(session, row, engine, "first")
        assert row.status == "RUNNING" and row.state["funded"]
        state = json.dumps(row.state, sort_keys=True)
        row.status = "PAUSED"
    async with engine.sessions.begin() as session:
        row = await session.get(Strategy, row.id)
        await start(session, row, engine, "resume")
        assert json.dumps(row.state, sort_keys=True) == state
    engine.upbit.place.assert_not_awaited()
    engine.upbit.cancel.assert_not_awaited()


@pytest.mark.parametrize("obstruction", ["quantity", "locked", "price", "stale", "manual", "funded"])
async def test_import_rechecks_account_and_never_modifies_draft_on_failure(db, obstruction):
    engine = await engine_fixture(db)
    async with engine.sessions.begin() as session:
        row = await create_strategy(session, "fixture", spec(), "live")
        if obstruction == "quantity":
            engine.upbit.chance.return_value = chance(balance="7")
        elif obstruction == "locked":
            engine.upbit.chance.return_value = chance(balance="5.00000001", locked="1")
        elif obstruction == "price":
            engine.upbit.orderbooks.return_value = [quote("99000")]
        elif obstruction == "stale":
            engine.upbit.orderbooks.return_value = [
                Quote("KRW-SOL", D(100000), D(100000), datetime.now(UTC) - timedelta(minutes=1))
            ]
        elif obstruction == "manual":
            engine.upbit.orders.return_value = [{"symbol": "KRW-SOL", "orderId": "manual"}]
        elif obstruction == "funded":
            row.state = {**funded_state(spec()), "funded": True}
            await session.flush()
            with pytest.raises(ValueError, match="이미 운용"):
                await engine.prepare_inventory(session, request().model_dump(mode="json"))
            return
        with pytest.raises(ValueError):
            await engine.check_live_start(row)
        assert not row.state["funded"] and row.state["quantity"] == "0"
    engine.upbit.place.assert_not_awaited()


async def test_waiting_below_first_buy_does_not_halt_but_account_loss_still_does(db):
    engine = await engine_fixture(db)
    async with engine.sessions.begin() as session:
        row = await create_strategy(session, "fixture", spec(), "live")
    async with engine.sessions.begin() as session:
        row = await session.get(Strategy, row.id)
        await start(session, row, engine, "start")
        await check_risk(session, engine.settings, {"KRW-SOL": quote()}, "2026-01-01", "upbit")
        assert row.status == "RUNNING"  # Current price is below the lowest grid pair.
        await check_risk(session, engine.settings, {"KRW-SOL": quote("98000")}, "2026-01-01", "upbit")
        assert row.status == "PAUSED" and "손실" in row.reason


async def test_preparation_must_be_handled_by_engine(db):
    engine = await engine_fixture(db)
    async with engine.sessions.begin() as session:
        command = await enqueue(
            session, "prepare", "prepare_inventory", request().model_dump(mode="json"), "fixture"
        )
        with pytest.raises(ValueError, match="엔진"):
            await process_command(session, command, engine.settings)


async def test_partial_cancel_and_late_fill_update_only_the_owned_slot_once(db):
    engine = await engine_fixture(db)
    s = spec()
    async with engine.sessions.begin() as session:
        row = await create_strategy(session, "fixture", s, "live")
        row.state = {**funded_state(s), "funded": True}
        order = Intent(
            strategy_id=row.id,
            mode="live",
            venue="upbit",
            symbol=s.symbol,
            side="SELL",
            quantity=grid_quantity(s, 0),
            price=levels(s)[1],
            slot=0,
            status="PENDING",
        )
        session.add(order)
        await session.flush()
        for size in ("0.5", "0.5", "0.7", "0.7"):
            proceeds = order.price * D(size)
            await record_execution(
                session,
                order,
                {
                    "symbol": s.symbol,
                    "side": "SELL",
                    "status": "CANCELED",
                    "execution": {
                        "filledQuantity": size,
                        "filledAmount": str(proceeds),
                        "commission": str(proceeds * D("0.0005")),
                        "tax": "0",
                    },
                },
            )
        assert D(row.state["quantity"]) == s.inventory_quantity - D("0.7")
        assert D(row.state["lots"]["0"]["cash"]) == order.price * D("0.7") * D("0.9995")
        assert all(lot["cash"] == "0" for i, lot in row.state["lots"].items() if i != "0")
        retry = decide(s, json.loads(json.dumps(row.state)), quote(str(levels(s)[0])), [])[0]
        assert retry.side == "BUY" and retry.quantity == D("0.7")


async def test_resume_with_small_coin_remainder_tests_funded_buyback(db):
    engine = await engine_fixture(db)
    s = spec()
    state = funded_state(s)
    for i in range(s.grids):
        size = grid_quantity(s, i) - (D("0.00001") if i == 0 else D(0))
        proceeds = size * levels(s)[i + 1]
        apply_fill(state, "SELL", size, proceeds, proceeds * D("0.0005"), i)
    state["funded"] = True
    engine.upbit.chance.return_value = chance(balance="0.00001", cash=state["cash"])
    row = Strategy(venue="upbit", mode="live", symbol=s.symbol, config=s.model_dump(mode="json"), state=state)
    await engine.check_live_start(row)
    assert engine.upbit.test_order.await_count == s.grids
    assert all(call.args[0].side == "BUY" for call in engine.upbit.test_order.await_args_list)
