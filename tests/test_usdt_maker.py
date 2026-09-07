"""Currency, inventory ownership and resting-order lifecycle regressions. No network."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select
from test_inventory_grid import chance as krw_chance
from test_inventory_grid import engine_fixture
from test_inventory_grid import quote as krw_quote
from test_inventory_grid import spec as krw_spec

from cotrader.account import account_view
from cotrader.broker import BrokerError
from cotrader.config import Settings
from cotrader.domain import D, Quote, apply_fill, decide, funded_state, grid_quantity, levels
from cotrader.live import InventoryGridRequest, inventory_grid_spec
from cotrader.markets import asset_key, currency, order_size_valid, price_tick, round_quantity, tick_size
from cotrader.models import Intent, Ledger, RuntimeState, Strategy, now
from cotrader.services import (
    approval_digest,
    check_risk,
    create_strategy,
    enqueue,
    process_command,
)
from cotrader.upbit import UpbitBroker


def chance(**changes):
    c = krw_chance(balance="5.5", average="125000", **changes)
    c["market"].update(id="USDT-SOL", bid={"min_total": "0.5"}, ask={"min_total": "0.5"})
    c["bid_account"]["currency"] = "USDT"
    c["ask_account"]["unit_currency"] = "KRW"
    c.update(maker_bid_fee="0", maker_ask_fee="0")
    return c


def quote(bid="106.10", ask="106.16"):
    return Quote("USDT-SOL", D(bid), D(ask), datetime.now(UTC), D(100), D(100))


def request(**changes):
    return InventoryGridRequest.model_validate(
        {
            "symbol": "USDT-SOL",
            "allocation": "all",
            "first_sell_basis": "near_market",
            "execution_policy": "maker_only",
            "step_percent": "0.75",
            **changes,
        }
    )


def spec():
    return inventory_grid_spec(request(), chance(), quote())


async def fixture(db):
    engine = await engine_fixture(db)
    engine.settings.upbit_usdt_live_enabled = True
    async with engine.sessions.begin() as session:
        from cotrader.models import AccountState

        (await session.get(AccountState, {"mode": "live", "venue": "upbit_usdt"})).capital = D(1000)
    engine.upbit.chance.return_value = chance()
    engine.upbit.orderbooks.side_effect = lambda symbols: [
        quote() if symbols[0] == "USDT-SOL" else krw_quote()
    ]
    engine.upbit.markets.return_value["USDT-SOL"] = engine.upbit.markets.return_value["KRW-SOL"]
    engine.upbit_markets = engine.upbit.markets.return_value
    engine.quotes = {"USDT-SOL": quote(), "KRW-SOL": krw_quote()}
    return engine


@pytest.mark.parametrize(
    "price, step",
    [
        ("106.169", ".01"),
        ("10", ".01"),
        ("1", ".001"),
        (".1", ".0001"),
        (".01", ".00001"),
        (".001", ".000001"),
        (".0001", ".0000001"),
        (".00001", ".00000001"),
    ],
)
def test_usdt_tick_and_minimum(price, step):
    assert tick_size(D(price), "upbit_usdt") == D(step)
    assert price_tick(D(price), "upbit_usdt") % D(step) == 0
    assert order_size_valid(D("0.005"), D(100), "upbit_usdt")
    assert not order_size_valid(D("0.00499999"), D(100), "upbit_usdt")
    assert round_quantity(D("1.123456789"), "upbit_usdt") == D("1.12345678")


def test_currency_and_draft_do_not_convert_account_average_or_invent_cash():
    s = spec()
    assert s.venue == "upbit_usdt" and currency(s.venue) == "USDT"
    assert levels(s)[1] == D("106.17")
    assert s.inventory_average_currency == "KRW" and s.inventory_average_price == D("125000")
    assert s.commission_rate == D(".0025")
    assert s.budget == D("5.5") * D("106.10")
    assert funded_state(s)["cash"] == "0"
    assert asset_key("upbit", "KRW-SOL") == asset_key("upbit_usdt", "USDT-SOL")
    with pytest.raises(ValueError, match="다른 통화"):
        inventory_grid_spec(request(first_sell_basis="average"), chance(), quote())


@pytest.mark.parametrize("bid", ["100.01", "106.10", "999.99"])
def test_one_tick_start_survives_price_rounding(bid):
    q = quote(bid, bid)
    s = inventory_grid_spec(request(), chance(), q)
    assert levels(s)[1] == q.ask + tick_size(q.ask, s.venue)


def test_resting_slots_and_partial_fill_restart_never_duplicate_or_cross():
    s = spec()
    state = funded_state(s)
    blocked = set()
    for i in range(5):
        d = decide(s, state, quote(), [], blocked_slots=blocked)[0]
        assert d.side == "SELL" and d.slot == i
        assert d.price > quote().ask
        blocked.add(i)
    assert decide(s, state, quote(), [], blocked_slots=blocked)[0] is None
    size = grid_quantity(s, 0)
    apply_fill(state, "SELL", size / 2, size / 2 * levels(s)[1], D(0), 0)
    state = json.loads(json.dumps(state))
    assert decide(s, state, quote(), [], blocked_slots=blocked)[0] is None
    apply_fill(state, "SELL", size / 2, size / 2 * levels(s)[1], D(0), 0)
    blocked.remove(0)
    buy = decide(s, state, quote(), [], blocked_slots=blocked)[0]
    assert buy.side == "BUY" and buy.quantity == size and buy.price == levels(s)[0]
    apply_fill(state, "BUY", size, size * buy.price, D(0), 0)
    sell = decide(s, json.loads(json.dumps(state)), quote(), [], blocked_slots=blocked)[0]
    assert sell.side == "SELL" and sell.price == levels(s)[1]
    crossed = quote(str(levels(s)[-1] + 1), str(levels(s)[-1] + 1))
    assert decide(s, state, crossed, [])[0] is None
    assert D(state["cash"]) == size * (levels(s)[1] - levels(s)[0])


async def test_usdt_post_only_test_payload_and_independent_market_gate():
    seen = []

    async def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"uuid": "fixture", "identifier": "fixture"})

    settings = Settings(
        runtime_role="engine",
        auth_mode="github",
        upbit_enabled=True,
        upbit_live_enabled=True,
        UPBIT_OPEN_API_ACCESS_KEY="fixture-access",
        UPBIT_OPEN_API_SECRET_KEY="fixture-secret",
    )
    broker = UpbitBroker(
        settings, httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://fixture")
    )
    intent = Intent(
        id="fixture",
        venue="upbit_usdt",
        symbol="USDT-SOL",
        side="SELL",
        price=D("106.17"),
        quantity=D("1.1"),
    )
    try:
        await broker.test_order(intent, maker_only=True)
        payload = json.loads(seen[0].content)
        assert seen[0].url.path == "/v1/orders/test"
        assert (
            payload["time_in_force"] == "post_only"
            and payload["ord_type"] == "limit"
            and "smp_type" not in payload
        )
        with pytest.raises(BrokerError, match="market-read-only"):
            await broker.place(intent, maker_only=True)
        assert len(seen) == 1
    finally:
        await broker.close()


async def test_transfer_preserves_books_and_requires_user_start(db):
    engine = await fixture(db)
    old = krw_spec().model_copy(update={"inventory_quantity": D("5.5"), "budget": D("550000")})
    async with engine.sessions.begin() as session:
        source = await create_strategy(session, "source", old, "live")
        source.state = {**funded_state(old), "funded": True, "cash": "123", "realized": "123"}
        source.status = "PAUSED"
        before = dict(source.state)
        target = await engine.prepare_inventory(
            session,
            request(
                source_strategy_id=source.id,
                source_version=source.version,
                source_approval=approval_digest(source, engine.settings),
            ).model_dump(mode="json"),
        )
        assert source.status == "ARCHIVED" and not source.state["funded"]
        assert source.state["cash"] == before["cash"] and source.state["realized"] == before["realized"]
        assert source.state["released_value"] == "550000.0" and source.state["quantity"] == "0"
        assert target.status == "DRAFT" and not target.state["funded"] and target.state["quantity"] == "0"
        assert target.state["transferred_from"] == source.id
        assert await session.scalar(select(func.count()).select_from(Intent)) == 0
        assert await session.scalar(select(func.count()).select_from(Ledger)) == 0
        await check_risk(session, engine.settings, engine.quotes, "2026-09-07", "upbit")
        portfolio = await session.get(RuntimeState, "portfolio:upbit:live")
        assert D(portfolio.data["equity"]) == engine.settings.capital_krw + 123
        assert D(portfolio.data["released_value"]) == 550000
    assert all(c.kwargs["maker_only"] for c in engine.upbit.test_order.await_args_list)
    engine.upbit.place.assert_not_awaited()
    engine.upbit.cancel.assert_not_awaited()


@pytest.mark.parametrize(
    "obstruction",
    [
        "running",
        "approval",
        "version",
        "quantity",
        "pending",
        "locked",
        "foreign",
        "budget",
        "halted",
        "test_error",
    ],
)
async def test_transfer_failure_keeps_original_book(db, obstruction):
    engine = await fixture(db)
    old = krw_spec().model_copy(update={"inventory_quantity": D("5.5"), "budget": D("550000")})
    async with engine.sessions.begin() as session:
        source = await create_strategy(session, "source", old, "live")
        source.state = {**funded_state(old), "funded": True}
        source.status = "PAUSED"
        payload = request(
            source_strategy_id=source.id,
            source_version=source.version,
            source_approval=approval_digest(source, engine.settings),
        ).model_dump(mode="json")
        if obstruction == "running":
            source.status = "RUNNING"
        if obstruction == "approval":
            payload["source_approval"] = "wrong"
        if obstruction == "version":
            payload["source_version"] += 1
        if obstruction == "quantity":
            source.state = {**source.state, "quantity": "4"}
        if obstruction == "pending":
            session.add(
                Intent(
                    strategy_id=source.id,
                    venue="upbit",
                    mode="live",
                    symbol="KRW-SOL",
                    side="SELL",
                    price=D(100000),
                    quantity=D(1),
                    status="UNKNOWN",
                )
            )
            await session.flush()
        if obstruction == "locked":
            engine.upbit.chance.return_value = chance(locked="1")
        if obstruction == "foreign":
            engine.upbit.orders.return_value = [{"symbol": "KRW-SOL", "orderId": "foreign"}]
        if obstruction == "budget":
            from cotrader.models import AccountState

            (await session.get(AccountState, {"mode": "live", "venue": "upbit_usdt"})).capital = D(1)
        if obstruction == "halted":
            from cotrader.models import AccountState

            (await session.get(AccountState, {"mode": "live", "venue": "upbit_usdt"})).halted = True
        if obstruction == "test_error":
            engine.upbit.test_order.side_effect = BrokerError("test-failure")
        before = json.dumps(source.state, sort_keys=True)
        with pytest.raises((ValueError, BrokerError)):
            await engine.prepare_inventory(session, payload)
        assert json.dumps(source.state, sort_keys=True) == before and source.status != "ARCHIVED"
        assert await session.scalar(select(func.count()).select_from(Strategy)) == 1
    engine.upbit.place.assert_not_awaited()
    engine.upbit.cancel.assert_not_awaited()


async def test_start_cannot_double_allocate_sol_across_quotes(db):
    engine = await fixture(db)
    async with engine.sessions.begin() as session:
        source = await create_strategy(session, "source", krw_spec(), "live")
        source.state = {**funded_state(krw_spec()), "funded": True}
        target = await create_strategy(session, "target", spec(), "live")
        c = await enqueue(
            session,
            "fixture",
            "start",
            {
                "strategy_id": target.id,
                "version": target.version,
                "approval": approval_digest(target, engine.settings),
            },
            "fixture",
        )
        with pytest.raises(ValueError, match="같은 종목"):
            await process_command(session, c, engine.settings, live_checked=True)
        assert not target.state["funded"]


async def running(engine):
    async with engine.sessions.begin() as session:
        row = await create_strategy(session, "fixture", spec(), "live")
        row.status = "RUNNING"
        row.state = {**funded_state(spec()), "funded": True}
        await check_risk(session, engine.settings, engine.quotes, "2026-09-07", "upbit_usdt")
        return row.id


async def test_engine_queues_each_free_slot_and_keeps_resting_orders(db):
    engine = await fixture(db)
    owner_id = await running(engine)
    for _ in range(7):
        await engine.evaluate(datetime.now(UTC))
    async with engine.sessions.begin() as session:
        intents = (await session.scalars(select(Intent))).all()
        assert len(intents) == 5 and {r.slot for r in intents} == set(range(5))
        for r in intents:
            r.status = "PENDING"
            r.broker_id = r.id
            r.created_at = now() - timedelta(hours=2)
    await engine.cancel_stopped()
    engine.upbit.cancel.assert_not_awaited()
    async with engine.sessions() as session:
        assert all(r.status == "PENDING" for r in (await session.scalars(select(Intent))).all())
        assert (await session.get(Strategy, owner_id)).status == "RUNNING"


@pytest.mark.parametrize("case", ["normal", "crossed", "self_cross", "fee", "timeout", "recovery"])
async def test_dispatch_maker_guards_and_no_ambiguous_retry(db, case):
    engine = await fixture(db)
    owner_id = await running(engine)
    async with engine.sessions.begin() as session:
        intent = Intent(
            strategy_id=owner_id,
            venue="upbit_usdt",
            mode="live",
            symbol="USDT-SOL",
            side="SELL",
            price=D("106.17"),
            quantity=D("1.1"),
            slot=0,
        )
        session.add(intent)
        await session.flush()
        iid = intent.id
        if case == "recovery":
            intent.status = "UNKNOWN"
    if case == "crossed":
        engine.quotes["USDT-SOL"] = quote("106.18", "106.20")
    if case == "self_cross":
        engine.upbit.orders.return_value = [
            {"orderId": "own", "symbol": "USDT-SOL", "side": "BUY", "price": "106.17"}
        ]
    if case == "fee":
        engine.upbit.chance.return_value["maker_ask_fee"] = "0.003"
    if case == "timeout":
        engine.upbit.place.side_effect = BrokerError("timeout", ambiguous=True)
    else:
        engine.upbit.place.return_value = {"orderId": "fixture"}
    await engine.submit_live(iid, recovery=case == "recovery")
    async with engine.sessions() as session:
        state = (await session.get(Intent, iid)).status
        assert (
            state
            == {
                "normal": "PENDING",
                "crossed": "CANCELED",
                "self_cross": "CANCELED",
                "fee": "REJECTED",
                "timeout": "UNKNOWN",
                "recovery": "UNKNOWN",
            }[case]
        )
        if case == "fee":
            assert (await session.get(Strategy, owner_id)).status == "PAUSED"
    if case in {"normal", "timeout"}:
        assert engine.upbit.place.await_args.kwargs == {"maker_only": True}
    else:
        engine.upbit.place.assert_not_awaited()
    if case == "timeout":
        await engine.submit_live(iid, recovery=True)
        assert engine.upbit.place.await_count == 1


def test_account_views_are_currency_specific_and_missing_is_not_zero():
    row = RuntimeState(
        data={
            "status": "CONNECTED",
            "snapshot": {
                "checked_at": datetime.now(UTC).isoformat(),
                "cash_available": "5000",
                "cash_locked": "0",
                "balances": [
                    {"currency": "KRW", "balance": "5000", "locked": "0"},
                    {"currency": "USDT", "balance": "23.45", "locked": "1"},
                ],
            },
        }
    )
    assert account_view(row, "upbit_usdt")["snapshot"]["cash_available"] == "23.45"
    assert account_view(row, "upbit")["snapshot"]["cash_available"] == "5000"
    del row.data["snapshot"]["balances"]
    assert account_view(row, "upbit_usdt")["snapshot"] is None


def test_coarse_tick_does_not_bypass_cost_margin_to_force_a_draft():
    with pytest.raises(ValueError, match="간격"):
        inventory_grid_spec(request(), chance(), quote("10.00", "10.00"))
