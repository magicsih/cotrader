from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from cotrader.api import create_app
from cotrader.config import Settings
from cotrader.db import SingleWriter
from cotrader.domain import D, Quote, StrategySpec
from cotrader.models import AccountState, Command, Intent, Ledger, RuntimeState, Strategy, now
from cotrader.services import (
    approval_digest,
    bootstrap,
    check_risk,
    create_strategy,
    enqueue,
    process_command,
    record_execution,
)


async def strategy_row(sessions, mode="paper"):
    settings = Settings()
    async with sessions.begin() as session:
        await bootstrap(session, settings)
        row = await create_strategy(
            session,
            "테스트 그리드",
            StrategySpec(symbol="TEST", budget="1000", lower="90", upper="110", grids=4),
            mode,
        )
        await session.flush()
        command = await enqueue(
            session,
            "test-start",
            "start",
            {"strategy_id": row.id, "version": 1, "approval": approval_digest(row, settings)},
            "local",
        )
        if mode == "paper":
            await process_command(session, command, settings)
        return row.id


async def test_command_idempotency_and_version_binding(db):
    _, sessions = db
    row_id = await strategy_row(sessions)
    async with sessions.begin() as session:
        original = await session.get(Command, "test-start")
        duplicate = await enqueue(session, "test-start", "start", original.payload, "local")
        assert duplicate is original
        with pytest.raises(ValueError, match="내용이 다릅니다"):
            await enqueue(session, "test-start", "pause_all", {}, "local")
        wrong = await enqueue(session, "wrong", "start", {"strategy_id": row_id, "version": 2}, "local")
        with pytest.raises(ValueError, match="버전"):
            await process_command(session, wrong, Settings())


async def test_partial_fill_duplicates_reordered_events_and_fee_finalization(db):
    _, sessions = db
    row_id = await strategy_row(sessions)
    async with sessions.begin() as session:
        intent = Intent(
            strategy_id=row_id,
            mode="paper",
            symbol="TEST",
            side="BUY",
            quantity=D(2),
            price=D(95),
            slot=1,
            status="PENDING",
        )
        session.add(intent)
        await session.flush()
        order = {
            "symbol": "TEST",
            "side": "BUY",
            "status": "PARTIAL_FILLED",
            "execution": {"filledQuantity": "1", "filledAmount": "95", "commission": None, "tax": None},
        }
        await record_execution(session, intent, order)
        await session.flush()
        await record_execution(session, intent, order)
        assert await session.scalar(select(func.count()).select_from(Ledger)) == 1
        row = await session.get(Strategy, row_id)
        assert D(row.state["quantity"]) == 1
        assert not intent.costs_final
        terminal = {
            **order,
            "status": "CANCELED",
            "execution": {"filledQuantity": "1", "filledAmount": "95", "commission": "0.05", "tax": "0"},
        }
        await record_execution(session, intent, terminal)
        assert intent.costs_final and intent.status == "CANCELED"
        assert D(row.state["cash"]) == D("904.95")
        await record_execution(
            session, intent, {**order, "execution": {"filledQuantity": "0", "filledAmount": None}}
        )
        assert D(row.state["quantity"]) == 1
        await record_execution(session, intent, order)
        assert intent.costs_final and intent.costs == D("0.05")
        assert D(row.state["cash"]) == D("904.95")


async def test_risk_includes_unrealized_losses_and_is_persistent(db):
    _, sessions = db
    row_id = await strategy_row(sessions)
    async with sessions.begin() as session:
        row = await session.get(Strategy, row_id)
        row.state = {**row.state, "cash": "0", "quantity": "10", "cost_basis": "1000"}
        quote = Quote("TEST", D(90), D(90), datetime.now(UTC))
        await check_risk(session, Settings(), {"TEST": quote}, "")
    async with sessions() as session:
        account = await session.get(AccountState, "paper")
        row = await session.get(Strategy, row_id)
        assert account.halted and row.status == "PAUSED"
        assert D(row.state["quantity"]) == 10


async def test_missing_market_data_is_not_zero_equity(db):
    _, sessions = db
    row_id = await strategy_row(sessions)
    async with sessions.begin() as session:
        row = await session.get(Strategy, row_id)
        row.state = {**row.state, "cash": "900", "quantity": "1", "cost_basis": "100"}
        await check_risk(session, Settings(), {}, "")
        snapshot = await session.get(RuntimeState, "portfolio:paper")
        assert snapshot.data["equity"] is None
        assert not snapshot.data["complete"]


async def test_wide_spread_and_pending_order_do_not_hide_lower_grid_breach(db):
    _, sessions = db
    row_id = await strategy_row(sessions)
    async with sessions.begin() as session:
        row = await session.get(Strategy, row_id)
        row.state = {**row.state, "cash": "800", "quantity": "2", "cost_basis": "200"}
        session.add(
            Intent(
                strategy_id=row_id,
                mode="paper",
                symbol="TEST",
                side="BUY",
                quantity=D(1),
                price=D(90),
                status="PENDING",
            )
        )
        await check_risk(session, Settings(), {"TEST": Quote("TEST", D(80), D(98), datetime.now(UTC))}, "")
        assert row.status == "PAUSED" and "하단 이탈" in row.reason
        portfolio = await session.get(RuntimeState, "portfolio:paper")
        assert portfolio.data["complete"] and D(portfolio.data["equity"]) == 4960


async def test_old_approval_cannot_start_after_restart(db):
    _, sessions = db
    row_id = await strategy_row(sessions)
    async with sessions.begin() as session:
        command = await enqueue(session, "old", "start", {"strategy_id": row_id, "version": 1}, "local")
        command.created_at = now() - timedelta(minutes=6)
        with pytest.raises(ValueError, match="지연"):
            await process_command(session, command, Settings())


async def test_mysql_single_writer_lock(db):
    engine, _ = db
    if engine.dialect.name != "mysql":
        pytest.skip("MySQL 통합 실행에서 검증")
    async with SingleWriter(engine, "cotrader:test") as first:
        await first.verify()
        with pytest.raises(RuntimeError, match="실행권"):
            async with SingleWriter(engine, "cotrader:test"):
                pass
    async with SingleWriter(engine, "cotrader:test") as second:
        await second.verify()


async def test_approval_is_invalidated_by_risk_setting_change(db):
    _, sessions = db
    row_id = await strategy_row(sessions)
    async with sessions.begin() as session:
        row = await session.get(Strategy, row_id)
        command = await enqueue(
            session,
            "risk-changed",
            "start",
            {"strategy_id": row_id, "version": 1, "approval": approval_digest(row, Settings())},
            "local",
        )
        with pytest.raises(ValueError, match="위험 한도"):
            await process_command(session, command, Settings(daily_loss_usd="100"))


async def test_archive_releases_budget_without_erasing_realized_losses(db):
    _, sessions = db
    row_id = await strategy_row(sessions)
    async with sessions.begin() as session:
        row = await session.get(Strategy, row_id)
        row.status = "PAUSED"
        row.state = {**row.state, "cash": "980", "realized": "-19", "costs": "1"}
        command = await enqueue(session, "finish", "archive", {"strategy_id": row_id}, "local")
        await process_command(session, command, Settings())
        await check_risk(session, Settings(), {}, "")
        portfolio = await session.get(RuntimeState, "portfolio:paper")
        assert D(portfolio.data["equity"]) == 4980
        assert D(portfolio.data["realized_gross"]) == -19
        assert row.status == "ARCHIVED" and not row.state["funded"]
        new = await create_strategy(
            session,
            "다음 전략",
            StrategySpec(symbol="NEXT", budget="5000", lower="90", upper="110", grids=4),
            "paper",
        )
        start = await enqueue(
            session,
            "next",
            "start",
            {"strategy_id": new.id, "version": 1, "approval": approval_digest(new, Settings())},
            "local",
        )
        with pytest.raises(ValueError, match="총예산"):
            await process_command(session, start, Settings())


async def test_api_is_local_only_and_roundtrips_strategy(db):
    _, sessions = db
    app = create_app(Settings(), sessions_override=sessions)
    local = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)), base_url="http://127.0.0.1:8000"
    )
    stranger = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("10.0.0.8", 5000)), base_url="http://127.0.0.1:8000"
    )
    assert (await stranger.get("/api/strategies")).status_code == 403
    body = {
        "name": "첫 그리드",
        "mode": "paper",
        "spec": {"symbol": "TEST", "budget": "1000", "lower": "90", "upper": "110", "grids": 4},
    }
    assert (
        await local.post("/api/strategies", json=body, headers={"Origin": "https://untrusted.example"})
    ).status_code == 403
    preview = await local.post("/api/strategies/preview", json=body)
    assert preview.status_code == 200 and len(preview.json()["grid"]) == 4
    created = await local.post("/api/strategies", json=body)
    assert created.status_code == 201
    assert created.json()["status"] == "DRAFT"
    assert len((await local.get("/api/strategies")).json()) == 1
    await local.aclose()
    await stranger.aclose()
