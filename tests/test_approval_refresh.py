import json

import httpx
import pytest
from sqlalchemy import func, select
from test_market_cautions import VOLUME, change_policy
from test_usdt_maker import fixture, spec

from cotrader.api import create_app
from cotrader.config import Settings
from cotrader.models import Command, Event
from cotrader.services import approval_digest, create_strategy, enqueue


@pytest.mark.parametrize("allowed", [[], [VOLUME]])
async def test_repeated_policy_save_keeps_version_approval_and_financial_state(db, allowed):
    engine = await fixture(db)
    async with engine.sessions.begin() as session:
        row = await create_strategy(session, "fixture", spec(), "live")
        if allowed:
            await change_policy(session, row, engine.settings, allowed, "initial")
        before = json.dumps([row.config, row.state, row.status, row.version], sort_keys=True)
        approval = approval_digest(row, engine.settings)
        events = await session.scalar(select(func.count()).select_from(Event))
        command = await change_policy(session, row, engine.settings, allowed, "same-policy")
        assert command.status == "SUCCEEDED"
        assert json.dumps([row.config, row.state, row.status, row.version], sort_keys=True) == before
        assert approval_digest(row, engine.settings) == approval
        assert await session.scalar(select(func.count()).select_from(Event)) == events
    engine.upbit.place.assert_not_awaited()
    engine.upbit.cancel.assert_not_awaited()


async def test_pending_policy_and_command_result_remain_visible_beyond_recent_history(db):
    _, sessions = db
    settings = Settings(auth_mode="local")
    async with sessions.begin() as session:
        row = await create_strategy(session, "fixture", spec(), "paper")
        other = await create_strategy(session, "other", spec(), "paper")
        row_id, other_id = row.id, other.id
        await enqueue(session, "pending-policy", "set_market_cautions", {"strategy_id": row_id}, "local")
        for i in range(35):
            session.add(
                Command(
                    id=f"completed-{i}", action="check_live", payload={}, actor="local", status="SUCCEEDED"
                )
            )
    app = create_app(settings, sessions_override=sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 123)), base_url="http://localhost"
    ) as client:
        response = await client.get("/api/strategies")
        assert response.status_code == 200
        rows = {item["id"]: item for item in response.json()}
        assert rows[row_id]["pending_settings"] is True
        assert rows[other_id]["pending_settings"] is False
        assert (await client.get("/api/commands/pending-policy")).json()["status"] == "QUEUED"
        async with sessions.begin() as session:
            command = await session.get(Command, "pending-policy")
            command.status, command.result = "REJECTED", {"message": "fixture rejection"}
        assert (await client.get("/api/commands/pending-policy")).json()["result"][
            "message"
        ] == "fixture rejection"
        assert not any(item["pending_settings"] for item in (await client.get("/api/strategies")).json())
        assert (await client.get("/api/commands/missing")).status_code == 404
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.1", 123)), base_url="http://localhost"
    ) as client:
        assert (await client.get("/api/commands/pending-policy")).status_code == 403
