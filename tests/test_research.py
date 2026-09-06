import copy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from cotrader import research
from cotrader.api import create_app
from cotrader.config import Settings
from cotrader.domain import Bar, D, StrategySpec
from cotrader.models import BacktestJob, CandleRow
from cotrader.worker import compute_job


def candles(count=320, days=False):
    at = datetime(2026, 1, 1, tzinfo=UTC)
    result = []
    for i in range(count):
        close = D(95) + D(i % 40) / 2
        stamp = at + (timedelta(days=i // 10, minutes=i % 10) if days else timedelta(minutes=i))
        result.append(Bar(stamp, close, close + 1, close - 1, close, D(10000)))
    return result


def base():
    return StrategySpec(symbol="TEST", kind="trend", budget="1000", timeframe=1, fast=2, slow=4)


def test_optimizer_exhausts_valid_space_preserves_inputs_and_marks_synthetic():
    bars = candles()
    saved = copy.deepcopy(bars)
    updates = []
    result = research.optimize_grid(base(), bars, synthetic=True, progress=updates.append)
    expected, invalid = research.grid_search_space(base(), bars[:192])
    assert bars == saved
    assert result["tested_count"] == len(expected) > 6
    assert result["invalid_count"] == invalid
    profits = [D(c["training_profit"]) for c in result["candidates"]]
    assert profits == sorted(profits, reverse=True)
    assert len(profits) == 5
    assert result["selected_id"] in {c["id"] for c in result["candidates"]}
    assert updates[-1]["percent"] == 100
    assert not result["recommendation_ready"]
    assert any("가상 데이터" in s for s in result["recommendation_reasons"])


def test_final_period_cannot_change_selection_and_does_not_trigger_another_candidate(monkeypatch):
    bars = candles(days=True)
    final_start = bars[256].at
    calls = []

    def simulate(spec, segment, **kwargs):
        calls.append((spec.model_dump(), segment[0].at, kwargs))
        profit = str(spec.grids + (1 if spec.signal_gate else 0))
        if segment[0].at >= final_start and segment[-1].close > 1000:
            profit = "-10"
        return {"profit": profit, "max_drawdown": "2", "completed_cycles": 5, "halted": ""}

    monkeypatch.setattr(research, "backtest", simulate)
    good = research.optimize_grid(base(), bars, daily_loss=D(123), drawdown=D(456))
    assert good["recommendation_ready"]
    assert all(c[2]["daily_loss"] == 123 and c[2]["drawdown"] == 456 for c in calls)
    assert sum(c[1] == final_start for c in calls) == 1
    changed = [*bars[:-1], Bar(bars[-1].at, D(2000), D(2001), D(1999), D(2000), D(10000))]
    calls.clear()
    bad = research.optimize_grid(base(), changed)
    assert good["candidates"] == bad["candidates"]
    assert good["selected_id"] == bad["selected_id"]
    assert not bad["recommendation_ready"]
    assert any("마지막 구간" in reason for reason in bad["recommendation_reasons"])
    assert sum(c[1] == final_start for c in calls) == 1


def test_signal_precomputation_never_uses_future_buckets():
    bars = candles()
    short = research.backtest(base(), bars[:200])
    long = research.backtest(base(), bars)
    assert short["trades"]
    assert short["trades"] == [t for t in long["trades"] if t["at"] <= bars[199].at.isoformat()]
    with pytest.raises(ValueError, match="과거"):
        research.backtest(base(), bars[100:], warmup=bars[:101])


def test_optimizer_can_stop_without_finishing_the_space():
    with pytest.raises(research.ResearchCancelled):
        research.optimize_grid(base(), candles(), stop_requested=lambda: True)


async def test_api_cancels_queued_and_running_research_without_trade_commands(db):
    _, sessions = db
    app = create_app(Settings(), sessions_override=sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)), base_url="http://127.0.0.1:8000"
    ) as client:
        body = {
            "spec": base().model_dump(mode="json"),
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-02-01T00:00:00Z",
            "action": "suggest",
        }
        queued = (await client.post("/api/backtests", json=body)).json()
        assert queued["request"]["optimizer_version"] == 2
        canceled = await client.post(f"/api/backtests/{queued['id']}/cancel", json={})
        assert canceled.json()["status"] == "CANCELED"
        async with sessions.begin() as session:
            row = await session.get(BacktestJob, queued["id"])
            row.status = "RUNNING"
        assert (await client.post(f"/api/backtests/{queued['id']}/cancel", json={})).json()[
            "status"
        ] == "CANCEL_REQUESTED"
        assert (await client.get("/api/commands")).json() == []


async def test_worker_observes_cancel_request_even_when_computation_finishes(db):
    _, sessions = db
    bars = candles(4)
    async with sessions.begin() as session:
        for b in bars:
            session.add(
                CandleRow(
                    symbol="TEST",
                    interval="1m",
                    timestamp=b.at.replace(tzinfo=None),
                    data=b.json(),
                    source="synthetic",
                )
            )
        job = BacktestJob(
            status="CANCEL_REQUESTED",
            request={
                "spec": base().model_dump(mode="json"),
                "start": bars[0].at.isoformat(),
                "end": (bars[-1].at + timedelta(minutes=1)).isoformat(),
                "action": "backtest",
            },
        )
        session.add(job)
        await session.flush()
    with pytest.raises(research.ResearchCancelled):
        await compute_job(Settings(), sessions, job, AsyncMock())


@pytest.mark.parametrize("method", ["random", "tpe", "nsga2", "compare"])
def test_samplers_are_reproducible_and_holdout_is_not_used_to_tune(monkeypatch, method):
    bars = candles(days=True)
    calls = []

    def simulate(spec, segment, **kwargs):
        calls.append((segment[0].at, kwargs))
        return {
            "profit": str(spec.grids * 2 + int(spec.signal_gate)),
            "max_drawdown": str(spec.grids),
            "completed_cycles": 5,
            "halted": "",
        }

    monkeypatch.setattr(research, "backtest", simulate)
    options = research.OptimizationOptions(method=method, trials=30, seed=9, space="wide")
    result = research.optimize_grid(base(), bars, options=options, daily_loss=D(18), drawdown=D(90))
    assert result["possible_count"] == 756
    assert sum(at == bars[256].at for at, _ in calls) == 1
    assert all(arg["daily_loss"] == 18 and arg["drawdown"] == 90 for _, arg in calls)
    assert len(result["searches"]) == (3 if method == "compare" else 1)
    for search in result["searches"]:
        assert search["attempts"] == 30
        assert search["unique_count"] + search["invalid_attempts"] + search["duplicate_attempts"] == 30
        assert search["unique_count"] <= 30
    modified = [*bars[:-1], Bar(bars[-1].at, D(2000), D(2001), D(1999), D(2000), D(10000))]
    again = research.optimize_grid(base(), modified, options=options, daily_loss=D(18), drawdown=D(90))
    assert result["searches"] == again["searches"]
    assert result["selected_id"] == again["selected_id"]
    assert result["data_hash"] != again["data_hash"]


def test_risk_adjusted_score_changes_selection_without_changing_loss_limits(monkeypatch):
    def simulate(spec, segment, **kwargs):
        return {
            "profit": str(spec.grids),
            "max_drawdown": str(spec.grids * 2),
            "completed_cycles": 5,
            "halted": "",
        }

    monkeypatch.setattr(research, "backtest", simulate)
    profit = research.optimize_grid(base(), candles(days=True))
    cautious = research.optimize_grid(
        base(), candles(days=True), options=research.OptimizationOptions(drawdown_weight=1)
    )
    assert profit["candidates"][0]["spec"]["grids"] > cautious["candidates"][0]["spec"]["grids"]
    assert profit["assumptions"] == cautious["assumptions"]


async def test_api_persists_sampler_settings_and_rejects_unbounded_work(db):
    _, sessions = db
    app = create_app(Settings(), sessions_override=sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)), base_url="http://127.0.0.1:8000"
    ) as client:
        body = {
            "spec": base().model_dump(mode="json"),
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-02-01T00:00:00Z",
            "action": "suggest",
            "optimization": {
                "method": "nsga2",
                "space": "wide",
                "trials": 50,
                "seed": 23,
                "drawdown_weight": 1,
            },
        }
        response = await client.post("/api/backtests", json=body)
        assert response.status_code == 202
        assert response.json()["request"]["optimization"] == body["optimization"]
        body["optimization"]["trials"] = 100000
        assert (await client.post("/api/backtests", json=body)).status_code == 422
