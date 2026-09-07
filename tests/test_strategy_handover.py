"""Inactive book handover must not submit orders or erase financial history."""

import copy

import pytest
from sqlalchemy import func, select
from test_usdt_maker import fixture, quote, request, spec

from cotrader.broker import BrokerError
from cotrader.domain import D, funded_state
from cotrader.models import AccountState, Event, Intent, Ledger, RuntimeState, Strategy
from cotrader.services import approval_digest, check_risk, create_strategy, enqueue, process_command


async def books(engine, session):
    source = await create_strategy(session, "old grid", spec(), "live")
    source.status = "PAUSED"
    source.state = {
        **funded_state(spec()),
        "funded": True,
        "cash": "1.234567898",
        "realized": "2.34",
        "costs": "0.12",
    }
    target = await create_strategy(session, "replacement", spec(), "live")
    account = await session.get(AccountState, {"mode": "live", "venue": "upbit_usdt"})
    account.capital = D(600)  # Enough for one book, not both.
    payload = request(
        source_strategy_id=source.id,
        source_version=source.version,
        source_approval=approval_digest(source, engine.settings),
        target_strategy_id=target.id,
        target_version=target.version,
        target_approval=approval_digest(target, engine.settings),
    ).model_dump(mode="json")
    return source, target, payload


async def test_existing_draft_handover_preserves_equity_history_and_requires_new_start(db):
    engine = await fixture(db)
    engine.quotes["USDT-SOL"] = quote("106.20", "106.26")
    engine.upbit.orderbooks.side_effect = lambda _: [quote("106.20", "106.26")]
    # Provider cash may have fewer decimals than historical fill arithmetic.
    engine.upbit.chance.return_value["bid_account"]["balance"] = "1.23456789"
    async with engine.sessions.begin() as session:
        source, target, payload = await books(engine, session)
        source_before = copy.deepcopy(source.state)
        config_before = dict(target.config)
        await check_risk(session, engine.settings, engine.quotes, "2026-09-07", "upbit_usdt")
        portfolio = await session.get(RuntimeState, "portfolio:upbit_usdt:live")
        equity_before = portfolio.data["equity"]
        returned = await engine.prepare_inventory(session, payload)
        assert returned.id == target.id and target.version == 2
        assert target.status == "DRAFT" and not target.state["funded"]
        assert target.state["quantity"] == target.state["cash"] == "0"
        assert target.state["transferred_from"] == source.id
        assert source.status == "ARCHIVED" and not source.state["funded"]
        assert source.state["settled"] and source.state["transferred_to"] == target.id
        for key in ("cash", "costs", "realized"):
            assert source.state[key] == source_before[key]
        assert D(source.state["released_value"]) == D("5.5") * D("106.20")
        assert D(source.state["closed_unrealized"]) == D("0.55")
        assert {
            k: v for k, v in target.config.items() if k not in ("budget", "inventory_reference_price")
        } == {k: v for k, v in config_before.items() if k not in ("budget", "inventory_reference_price")}
        event = await session.scalar(select(Event).where(Event.kind == "transfer"))
        assert event.data["source_state"] == source_before
        assert event.data["actual_order_created"] is False
        assert await session.scalar(select(func.count()).select_from(Strategy)) == 2
        assert await session.scalar(select(func.count()).select_from(Intent)) == 0
        assert await session.scalar(select(func.count()).select_from(Ledger)) == 0
        await check_risk(session, engine.settings, engine.quotes, "2026-09-07", "upbit_usdt")
        assert portfolio.data["equity"] == equity_before
        # A stale start approval cannot start the updated draft.
        stale = await enqueue(
            session,
            "stale",
            "start",
            {
                "strategy_id": target.id,
                "version": 1,
                "approval": payload["target_approval"],
            },
            "fixture",
        )
        with pytest.raises(ValueError, match="버전"):
            await process_command(session, stale, engine.settings, live_checked=True)
        await engine.check_live_start(target, session)
        start = await enqueue(
            session,
            "new-start",
            "start",
            {
                "strategy_id": target.id,
                "version": target.version,
                "approval": approval_digest(target, engine.settings),
            },
            "fixture",
        )
        await process_command(session, start, engine.settings, live_checked=True)
        assert target.status == "RUNNING" and D(target.state["quantity"]) == D("5.5")
        await check_risk(session, engine.settings, engine.quotes, "2026-09-07", "upbit_usdt")
        assert portfolio.data["equity"] == equity_before
        assert portfolio.data["realized_gross"] == source_before["realized"]
    engine.upbit.place.assert_not_awaited()
    engine.upbit.cancel.assert_not_awaited()


@pytest.mark.parametrize(
    "obstruction",
    [
        "target_version",
        "target_approval",
        "target_running",
        "target_funded",
        "target_settled",
        "target_transferred",
        "target_cash",
        "target_quantity",
        "target_market",
        "target_history",
        "missing_source",
        "missing_target",
        "same_id",
        "source_running",
        "source_quantity",
        "pending",
        "locked",
        "market_warning",
        "test_failure",
        "capital",
        "settled_loss",
    ],
)
async def test_handover_failure_leaves_both_books_unchanged(db, obstruction):
    engine = await fixture(db)
    async with engine.sessions.begin() as session:
        source, target, payload = await books(engine, session)
        if obstruction == "target_version":
            payload["target_version"] += 1
        if obstruction == "target_approval":
            payload["target_approval"] = "wrong"
        if obstruction == "target_running":
            target.status = "RUNNING"
        for key in ("funded", "settled", "transferred"):
            if obstruction == f"target_{key}":
                target.state = {**target.state, "transferred_from" if key == "transferred" else key: True}
        for key in ("cash", "quantity"):
            if obstruction == f"target_{key}":
                target.state = {**target.state, key: "1"}
        if obstruction == "target_market":
            target.symbol = "USDT-BTC"
        if obstruction == "missing_source":
            payload["source_strategy_id"] = None
        if obstruction == "missing_target":
            payload["target_strategy_id"] = None
        if obstruction == "same_id":
            payload["target_strategy_id"] = source.id
        if obstruction == "source_running":
            source.status = "RUNNING"
        if obstruction == "source_quantity":
            source.state = {**source.state, "quantity": "4"}
        if obstruction in ("pending", "target_history"):
            session.add(
                Intent(
                    strategy_id=source.id if obstruction == "pending" else target.id,
                    venue="upbit_usdt",
                    mode="live",
                    symbol="USDT-SOL",
                    side="SELL",
                    price=D(106),
                    quantity=D(1),
                    status="UNKNOWN" if obstruction == "pending" else "CANCELED",
                )
            )
        if obstruction == "locked":
            engine.upbit.chance.return_value["ask_account"]["locked"] = "1"
        if obstruction == "market_warning":
            engine.upbit.markets.return_value["USDT-SOL"]["market_event"]["warning"] = True
        if obstruction == "test_failure":
            engine.upbit.test_order.side_effect = BrokerError("test-failure")
        if obstruction == "capital":
            (await session.get(AccountState, {"venue": "upbit_usdt", "mode": "live"})).capital = D(500)
        if obstruction == "settled_loss":
            lost = await create_strategy(session, "prior loss", spec(), "live")
            lost.state = {**lost.state, "settled": True}
            lost.status = "ARCHIVED"
        await session.flush()
        before = [
            (row.status, row.version, copy.deepcopy(row.config), copy.deepcopy(row.state))
            for row in (source, target)
        ]
        with pytest.raises((ValueError, BrokerError)):
            await engine.prepare_inventory(session, payload)
        assert [(r.status, r.version, r.config, r.state) for r in (source, target)] == before
        assert await session.scalar(select(func.count()).select_from(Event)) == 0
    engine.upbit.place.assert_not_awaited()
    engine.upbit.cancel.assert_not_awaited()


async def test_same_market_new_draft_and_repeated_handover_cannot_duplicate_ownership(db):
    engine = await fixture(db)
    async with engine.sessions.begin() as session:
        source, _, payload = await books(engine, session)
        for key in ("target_strategy_id", "target_version", "target_approval"):
            payload.pop(key)
        target = await engine.prepare_inventory(session, payload)
        count = await session.scalar(select(func.count()).select_from(Strategy))
        with pytest.raises(ValueError):
            await engine.prepare_inventory(session, payload)
        assert await session.scalar(select(func.count()).select_from(Strategy)) == count
        assert target.state["transferred_from"] == source.id and not source.state["funded"]
