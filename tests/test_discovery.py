import asyncio
import copy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import func, select

from cotrader import discovery, discovery_jobs, research
from cotrader.api import create_app
from cotrader.config import Settings
from cotrader.domain import Bar, D, StrategySpec
from cotrader.engine import Engine
from cotrader.models import CandleRow, Command, DiscoveryPlan, DiscoveryRun, Intent, Strategy, now
from cotrader.services import bootstrap, process_command
from cotrader.upbit import market_eligible


def bars():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        Bar(start + timedelta(days=i // 12, minutes=i % 12), D(100), D(102), D(98), D(100), D(10000))
        for i in range(360)
    ]


def request():
    return {
        **discovery_jobs.plan_request(Settings(market_source="toss"), "toss", D(5000), "drawdown"),
        "start": "2026-01-01T00:00:00+00:00",
        "end": "2026-01-31T00:00:00+00:00",
    }


def test_cross_symbol_selection_is_frozen_before_one_final_check(monkeypatch):
    series = bars()
    boundary = series[288].at
    calls = []

    def simulate(spec, segment, **kwargs):
        calls.append((spec, segment, kwargs))
        profit = "30" if spec.symbol == "QQQ" else "20"
        if segment[0].at >= boundary and segment[-1].close > 1000:
            profit = "-100"
        return {"profit": profit, "max_drawdown": "2", "completed_cycles": 5, "halted": ""}

    monkeypatch.setattr(discovery, "backtest", simulate)
    monkeypatch.setattr(research, "backtest", simulate)
    datasets = {s: {"bars": series, "sources": ["toss"]} for s in request()["symbols"]}
    good = discovery.discover(request(), datasets)
    assert good["recommendation_ready"] and good["selected"]["spec"]["symbol"] == "QQQ"
    assert sum(segment[0].at >= boundary for _, segment, _ in calls) == 1
    assert all(c[2]["daily_loss"] == 50 and c[2]["drawdown"] == 250 for c in calls)
    changed = copy.deepcopy(datasets)
    changed["QQQ"]["bars"] = [*series[:-1], Bar(series[-1].at, D(2000), D(2001), D(1999), D(2000), D(10000))]
    calls.clear()
    bad = discovery.discover(request(), changed)
    assert good["selected"]["id"] == bad["selected"]["id"]
    assert good["candidates"] == bad["candidates"]
    assert not bad["recommendation_ready"]
    assert sum(segment[0].at >= boundary for _, segment, _ in calls) == 1


def test_discovery_rejects_synthetic_and_aligns_missing_timestamps(monkeypatch):
    with pytest.raises(ValueError, match="실제 데이터"):
        discovery.discover(request(), {"SPY": {"bars": bars(), "sources": ["synthetic"]}})
    seen = []

    def simulate(spec, segment, **kwargs):
        seen.append((spec.symbol, segment[0].at, segment[-1].at))
        return {"profit": "2", "max_drawdown": "1", "completed_cycles": 4, "halted": ""}

    monkeypatch.setattr(discovery, "backtest", simulate)
    monkeypatch.setattr(research, "backtest", simulate)
    result = discovery.discover(
        request(),
        {"SPY": {"bars": bars(), "sources": ["toss"]}, "QQQ": {"bars": bars()[20:], "sources": ["toss"]}},
    )
    assert [d["count"] for d in result["datasets"]] == [340, 340]
    final_start = bars()[20:][272].at
    for symbol in ("SPY", "QQQ"):
        assert {start for s, start, _ in seen if s == symbol and start < final_start} == {
            bars()[20].at,
            bars()[224].at,
        }


async def test_discovery_api_scopes_cancel_and_saves_idempotent_paper_draft(db):
    _, sessions = db
    settings = Settings(market_source="toss", upbit_enabled=True)
    async with sessions.begin() as session:
        await bootstrap(session, settings)
    app = create_app(settings, sessions_override=sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)), base_url="http://127.0.0.1:8000"
    ) as client:
        body = {"venue": "upbit", "budget": "1000000"}
        response = await client.post("/api/discoveries", json=body)
        assert response.status_code == 202
        run = response.json()
        assert (await client.post("/api/discoveries", json=body)).status_code == 409
        assert (await client.post("/api/discoveries", json={**body, "budget": "1000001"})).status_code == 422
        listing = (await client.get("/api/discoveries?venue=upbit")).json()
        assert not listing["plan"]["enabled"]
        async with sessions.begin() as session:
            session.add(Command(id="unrelated", action="ingest", payload={}, actor="test"))
        assert (await client.post(f"/api/discoveries/{run['id']}/cancel", json={})).json()[
            "status"
        ] == "CANCELED"
        async with sessions.begin() as session:
            assert (await session.get(Command, "unrelated")).status == "QUEUED"
            for cid in run["request"]["commands"].values():
                assert (await session.get(Command, cid)).status == "CANCELED"
            row = await session.get(DiscoveryRun, run["id"])
            row.status, row.result = (
                "SUCCEEDED",
                {
                    "selected": {
                        "spec": StrategySpec(
                            venue="upbit", symbol="KRW-BTC", kind="trend", budget="1000000"
                        ).model_dump(mode="json")
                    },
                    "fingerprint": "test-result",
                },
            )
        draft = (await client.post(f"/api/discoveries/{run['id']}/draft", json={})).json()
        assert draft["mode"] == "paper" and draft["status"] == "DRAFT" and not draft["state"]["funded"]
        assert (await client.post(f"/api/discoveries/{run['id']}/draft", json={})).json()["id"] == draft["id"]
        # The same period and assumptions reuse the result without creating more collection commands.
        assert (await client.post("/api/discoveries", json=body)).json()["id"] == run["id"]
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Intent)) == 0
            assert await session.scalar(select(func.count()).select_from(Strategy)) == 1
            assert await session.scalar(select(func.count()).select_from(Command)) == 3
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("10.0.0.2", 5000)), base_url="http://127.0.0.1:8000"
    ) as client:
        assert (await client.get("/api/discoveries")).status_code == 403
        assert (await client.post("/api/discoveries", json=body)).status_code == 403


async def test_daily_plan_and_collection_timeout_only_affect_owned_runs(db):
    _, sessions = db
    settings = Settings(upbit_enabled=True)
    async with sessions.begin() as session:
        session.add(
            DiscoveryPlan(
                venue="upbit",
                enabled=True,
                next_run_at=now() - timedelta(seconds=1),
                request=discovery_jobs.plan_request(settings, "upbit", D(1000000), "drawdown"),
            )
        )
    await discovery_jobs.schedule_due(settings, sessions)
    await discovery_jobs.schedule_due(settings, sessions)
    async with sessions.begin() as session:
        rows = (await session.scalars(select(DiscoveryRun))).all()
        assert len(rows) == 1
        rows[0].created_at = now() - timedelta(hours=1)
        cid = next(iter(rows[0].request["commands"].values()))
        (await session.get(Command, cid)).status = "RUNNING"
    assert await discovery_jobs.collecting_run(settings, sessions) is None
    async with sessions() as session:
        assert (await session.get(DiscoveryRun, rows[0].id)).status == "FAILED"
        assert (await session.get(Command, cid)).status == "CANCELED"
        assert await session.scalar(select(func.count()).select_from(Strategy)) == 0
    await discovery_jobs.schedule_due(Settings(upbit_enabled=False), sessions)
    async with sessions.begin() as session:
        plan = await session.get(DiscoveryPlan, "upbit")
        plan.next_run_at = now() - timedelta(seconds=1)
    await discovery_jobs.schedule_due(Settings(upbit_enabled=False), sessions)
    async with sessions() as session:
        assert not (await session.get(DiscoveryPlan, "upbit")).enabled


async def test_ingestion_end_and_cancellation_during_network_read(db):
    database, sessions = db
    settings = Settings(market_source="toss")
    engine = Engine(settings)
    await engine.db.dispose()
    await engine.broker.close()
    await engine.upbit.close()
    engine.db, engine.sessions, engine.broker = database, sessions, AsyncMock()
    series = bars()
    payload = {
        "venue": "toss",
        "symbol": "SPY",
        "from": series[0].at.isoformat(),
        "to": series[1].at.isoformat(),
    }
    async with sessions.begin() as session:
        c = Command(id="test-ingest", action="ingest", actor="test", payload=payload)
        session.add(c)
        await session.flush()
        await process_command(session, c, settings)
        assert c.result["before"] == payload["to"]
    engine.broker.candles.return_value = {"candles": [{**b.json(), "currency": "USD"} for b in series[:3]]}
    await engine.ingest_page()
    async with sessions.begin() as session:
        assert await session.scalar(select(func.count()).select_from(CandleRow)) == 1
        row = await session.get(Command, c.id)
        row.status = "RUNNING"

    async def cancel_while_fetching(*args):
        async with sessions.begin() as session:
            (await session.get(Command, c.id)).status = "CANCELED"
        return {"candles": [{**b.json(), "currency": "USD"} for b in series[:3]]}

    engine.broker.candles.side_effect = cancel_while_fetching
    await engine.ingest_page()
    async with sessions() as session:
        assert (await session.get(Command, c.id)).status == "CANCELED"
        assert await session.scalar(select(func.count()).select_from(CandleRow)) == 1


def test_upbit_current_caution_flags_fail_closed():
    good = {
        "market_event": {
            "warning": False,
            "caution": {"PRICE_FLUCTUATIONS": False, "GLOBAL_PRICE_DIFFERENCES": False},
        }
    }
    assert market_eligible(good)
    assert not market_eligible({"market_warning": "NONE"})
    assert not market_eligible({"market_event": {"warning": False, "caution": {"PRICE_FLUCTUATIONS": True}}})
    assert not market_eligible({"market_event": {"warning": True, "caution": {"PRICE_FLUCTUATIONS": False}}})


async def test_compute_cancel_finishing_race_cannot_publish_success(db, monkeypatch):
    _, sessions = db
    req = {**request(), "commands": {"SPY": "missing", "QQQ": "missing"}}
    async with sessions.begin() as session:
        row = DiscoveryRun(venue="toss", request=req, status="CANCEL_REQUESTED")
        session.add(row)
        await session.flush()
    monkeypatch.setattr(discovery_jobs, "discover", lambda *args, **kwargs: {"selected": {}})
    with pytest.raises(research.ResearchCancelled):
        await discovery_jobs.compute_discovery(Settings(), sessions, row, AsyncMock())


async def test_daemon_sigterm_runs_cleanup(monkeypatch):
    import signal

    from cotrader.cli import run_daemon

    loop = asyncio.get_running_loop()
    callbacks, cleaned = {}, []
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, fn: callbacks.update({sig: fn}))
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: callbacks.pop(sig))

    async def work():
        try:
            await asyncio.sleep(60)
        finally:
            cleaned.append(True)

    task = asyncio.create_task(run_daemon(work()))
    await asyncio.sleep(0.01)
    callbacks[signal.SIGTERM]()
    await task
    assert cleaned == [True] and not callbacks
