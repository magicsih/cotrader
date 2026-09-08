import copy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from test_pockets import return_snapshot
from test_usdt_maker import fixture, quote, spec

from cotrader.domain import D, Quote, funded_state
from cotrader.models import Command, Event, Intent, Ledger, RuntimeState, Strategy
from cotrader.pockets import MigrationError, ledger_evidence, make_return_plan
from cotrader.retirement import retire_returned
from cotrader.services import check_risk, create_strategy


async def prepared(db):
    engine = await fixture(db)
    async with engine.sessions.begin() as session:
        source = await create_strategy(session, "ending grid", spec(), "live")
        source.status = "PAUSED"
        source.state = {
            **funded_state(spec()),
            "funded": True,
            "cash": "1.234567898",
            "realized": "2.34",
            "costs": "0.12",
        }
        identity = source.id
    snap = return_snapshot()
    snap["balances"]["target"] = {
        "SOL": {"balance": "5.5", "locked": "0"},
        "USDT": {"balance": "1.23456789", "locked": "0"},
        "APENFT": {"balance": "0.00000013", "locked": "0"},
    }
    plan = make_return_plan(snap, await ledger_evidence(engine.sessions), {}, ["APENFT"])
    return engine, identity, plan


async def test_retirement_preserves_profit_and_equity_without_orders(db):
    engine, identity, plan = await prepared(db)
    q = quote("106.20", "106.26")
    async with engine.sessions.begin() as session:
        source = await session.get(Strategy, identity)
        before = copy.deepcopy(source.state)
        await check_risk(session, engine.settings, {q.symbol: q}, "2026-09-08", "upbit_usdt")
        portfolio = await session.get(RuntimeState, "portfolio:upbit_usdt:live")
        equity = portfolio.data["equity"]
        await retire_returned(session, plan, {q.symbol: q}, datetime.now(UTC))
        assert source.status == "ARCHIVED" and not source.state["funded"]
        assert source.version == 2
        assert source.state["quantity"] == "0"
        assert D(source.state["released_quantity"]) == D("5.5")
        assert D(source.state["released_value"]) == D("584.10")
        for key in ["cash", "costs", "realized"]:
            assert source.state[key] == before[key]
        await check_risk(session, engine.settings, {q.symbol: q}, "2026-09-08", "upbit_usdt")
        assert portfolio.data["equity"] == equity
        event = await session.scalar(select(Event).where(Event.kind == "retirement"))
        assert event.data["source_state"] == before
        assert event.data["actual_order_created"] is False
        assert await session.scalar(select(func.count()).select_from(Intent)) == 0
        assert await session.scalar(select(func.count()).select_from(Ledger)) == 0
        with pytest.raises(MigrationError, match="장부가 변경"):
            await retire_returned(session, plan, {q.symbol: q}, datetime.now(UTC))


@pytest.mark.parametrize("failure", ["queued", "pending", "stale", "changed"])
async def test_retirement_requires_stable_book_no_orders_and_current_valuation(db, failure):
    engine, identity, plan = await prepared(db)
    q = quote("106.20", "106.26")
    async with engine.sessions.begin() as session:
        source = await session.get(Strategy, identity)
        if failure == "queued":
            session.add(Command(id="blocked", action="start", payload={}, actor="fixture"))
        elif failure == "pending":
            session.add(
                Intent(
                    strategy_id=identity,
                    venue="upbit_usdt",
                    mode="live",
                    symbol="USDT-SOL",
                    side="SELL",
                    quantity=D(1),
                    price=D(110),
                    status="UNKNOWN",
                )
            )
        elif failure == "changed":
            source.version += 1
        else:
            q = Quote(
                q.symbol, q.bid, q.ask, datetime.now(UTC) - timedelta(minutes=5), q.bid_size, q.ask_size
            )
        await session.flush()
        before = copy.deepcopy(source.state)
        with pytest.raises(MigrationError):
            await retire_returned(session, plan, {q.symbol: q}, datetime.now(UTC))
        assert source.status == "PAUSED" and source.state == before
