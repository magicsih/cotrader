import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select
from test_parallel_upbit_start import books, setup_pair

from cotrader.api import create_app
from cotrader.capital import capital_view
from cotrader.domain import D
from cotrader.models import AccountState, Event, Strategy, now
from cotrader.services import approval_digest, enqueue, process_command


async def request(session, settings, capital="3000"):
    preview = await capital_view(session, settings)
    return await enqueue(
        session,
        "capital-fixture",
        "set_usdt_capital",
        {"capital": capital, "approval": preview["approval"]},
        "fixture",
    )


async def test_confirmed_limit_preserves_orders_books_losses_and_invalidates_old_approvals(db):
    engine, source_id, target_id, _ = await setup_pair(db)
    async with engine.sessions.begin() as session:
        account = await session.get(AccountState, {"venue": "upbit_usdt", "mode": "live"})
        account.high_water = account.capital + 17
        account.daily_anchor = account.capital + 3
        account.halted, account.reason = True, "fixture loss"
        before = await books(session)
        rows = [await session.get(Strategy, strategy_id) for strategy_id in [source_id, target_id]]
        approvals = [approval_digest(row, engine.settings) for row in rows]
        versions = [row.version for row in rows]
        paper = await session.get(AccountState, {"venue": "upbit_usdt", "mode": "paper"})
        paper_before = (paper.capital, paper.high_water, paper.daily_anchor)
        command = await request(session, engine.settings)
        await process_command(session, command, engine.settings)
        assert command.status == "SUCCEEDED"
        assert await books(session) == before
        assert account.capital == D(3000)
        assert account.high_water - account.capital == 17
        assert account.daily_anchor - account.capital == 3
        assert account.halted and account.reason == "fixture loss"
        assert (paper.capital, paper.high_water, paper.daily_anchor) == paper_before
        assert [row.version for row in rows] == [v + 1 for v in versions]
        assert all(
            approval_digest(row, engine.settings) != old for row, old in zip(rows, approvals, strict=True)
        )
        event = await session.scalar(select(Event).where(Event.kind == "audit"))
        assert event.data["capital"] == "3000"
    engine.upbit.place.assert_not_awaited()
    engine.upbit.cancel.assert_not_awaited()


@pytest.mark.parametrize("obstruction", ["nan", "over", "below", "stale", "strategy", "expired", "extra"])
async def test_invalid_capital_change_does_not_partially_update_account(db, obstruction):
    engine, _, target_id, _ = await setup_pair(db)
    async with engine.sessions.begin() as session:
        command = await request(session, engine.settings)
        if obstruction in {"nan", "over", "below"}:
            command.payload = {
                **command.payload,
                "capital": {"nan": "NaN", "over": "10001", "below": "1"}[obstruction],
            }
        elif obstruction == "stale":
            command.payload = {**command.payload, "approval": "0" * 16}
        elif obstruction == "strategy":
            (await session.get(Strategy, target_id)).version += 1
        elif obstruction == "expired":
            command.created_at = now() - timedelta(minutes=6)
        elif obstruction == "extra":
            command.payload = {**command.payload, "reset_risk": True}
        account = await session.get(AccountState, {"venue": "upbit_usdt", "mode": "live"})
        before = (account.capital, account.high_water, account.daily_anchor, account.halted, account.reason)
        before_books = await books(session)
        with pytest.raises(ValueError):
            await process_command(session, command, engine.settings)
        assert (
            account.capital,
            account.high_water,
            account.daily_anchor,
            account.halted,
            account.reason,
        ) == before
        assert await books(session) == before_books
        assert await session.scalar(select(func.count()).select_from(Event)) == 0


async def test_same_capital_is_noop_and_preview_readback_uses_persisted_live_limit(db):
    engine, _, target_id, _ = await setup_pair(db)
    async with engine.sessions.begin() as session:
        preview = await capital_view(session, engine.settings)
        row = await session.get(Strategy, target_id)
        before = (row.version, json.dumps(row.state, sort_keys=True))
        await process_command(
            session, await request(session, engine.settings, preview["capital"]), engine.settings
        )
        assert (row.version, json.dumps(row.state, sort_keys=True)) == before
        assert await session.scalar(select(func.count()).select_from(Event)) == 0
    app = create_app(
        engine.settings.model_copy(update={"auth_mode": "local"}), sessions_override=engine.sessions
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 123)), base_url="http://localhost"
    ) as client:
        response = await client.get("/api/upbit-usdt-capital")
        assert response.status_code == 200
        assert D(response.json()["capital"]) == D(2000)
        assert len(response.json()["strategies"]) == 2
        live = await client.get("/api/status?venue=upbit_usdt&mode=live")
        paper = await client.get("/api/status?venue=upbit_usdt&mode=paper")
        assert D(live.json()["capital"]) == D(2000)
        assert D(paper.json()["capital"]) == engine.settings.capital_usdt
        async with engine.sessions.begin() as session:
            await enqueue(session, "pending-capital", "set_usdt_capital", {}, "fixture")
        rows = (await client.get("/api/strategies")).json()
        assert all(row["pending_settings"] for row in rows if row["mode"] == "live")
