import copy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import func, select

from cotrader import validation
from cotrader.api import create_app
from cotrader.config import Settings
from cotrader.domain import Bar, D, StrategySpec
from cotrader.models import BacktestJob, CandleRow, Command, Intent, Recommendation, Strategy
from cotrader.validation import RevalidationInput
from cotrader.worker import compute_job


def candidate(key="first"):
    spec = StrategySpec(symbol="TEST", budget="1000", lower="90", upper="110", grids=5)
    return {"id": key, "spec": spec.model_dump(mode="json")}


def source_job():
    c = candidate()
    return BacktestJob(
        status="SUCCEEDED",
        request={
            "action": "suggest",
            "spec": c["spec"],
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-02-01T00:00:00Z",
        },
        result={
            "optimizer_version": 2,
            "candidates": [c, candidate("second")],
            "selected_id": "first",
            "recommendation_ready": True,
            "recommendation_reasons": [],
            "data_hash": "training-data",
            "assumptions": {
                "daily_loss": "7",
                "drawdown": "13",
                "commission_rate": "0.001",
                "slippage_bps": "10",
            },
            "synthetic": False,
            "holdout": {"profit": "10"},
        },
    )


def body():
    return {
        "candidate_ids": ["first", "second"],
        "periods": [
            {"start": "2026-02-01T00:00:00Z", "end": "2026-03-01T00:00:00Z"},
            {"start": "2026-03-01T00:00:00Z", "end": "2026-04-01T00:00:00Z"},
        ],
    }


def client_for(sessions):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=create_app(Settings(daily_loss_usd=999, drawdown_usd=999), sessions_override=sessions),
            client=("127.0.0.1", 5000),
        ),
        base_url="http://127.0.0.1:8000",
    )


@pytest.mark.parametrize("change", ["overlap", "reverse", "duplicate", "future"])
def test_periods_must_be_past_ordered_and_non_overlapping(change):
    value = body()
    if change == "overlap":
        value["periods"][1]["start"] = "2026-02-15T00:00:00Z"
    if change == "reverse":
        value["periods"][0]["end"] = "2025-01-01T00:00:00Z"
    if change == "duplicate":
        value["candidate_ids"] = ["first", "first"]
    if change == "future":
        value["periods"][1]["end"] = "2099-01-01T00:00:00Z"
    with pytest.raises(ValueError):
        RevalidationInput.model_validate(value)


async def test_revalidation_freezes_source_and_rejects_reused_training_period(db):
    _, sessions = db
    async with sessions.begin() as session:
        source = source_job()
        session.add(source)
    async with client_for(sessions) as client:
        data = body()
        data["spec"] = {"budget": "5000"}
        response = await client.post(f"/api/backtests/{source.id}/revalidate", json=data)
        assert response.status_code == 202
        request = response.json()["request"]
        assert request["candidates"][0]["spec"]["budget"] == "1000"
        assert request["assumptions"]["daily_loss"] == "7"
        assert request["source_data_hash"] == "training-data"
        data["periods"][0]["start"] = "2026-01-31T23:59:59Z"
        assert (await client.post(f"/api/backtests/{source.id}/revalidate", json=data)).status_code == 422
        data = body()
        data["candidate_ids"] = ["forged"]
        assert (await client.post(f"/api/backtests/{source.id}/revalidate", json=data)).status_code == 422
        assert (
            await client.post(f"/api/backtests/{response.json()['id']}/revalidate", json=body())
        ).status_code == 422


def segment(month, source="toss"):
    start = datetime(2026, month, 1, tzinfo=UTC)
    bars = [
        Bar(start + timedelta(days=i // 20, minutes=i % 20), D(100), D(102), D(98), D(100), D(10000))
        for i in range(400)
    ]
    return {
        "start": start.isoformat(),
        "end": (start + timedelta(days=21)).isoformat(),
        "bars": bars,
        "warmup": [],
        "sources": [source],
    }


def request_data():
    source = source_job()
    return {
        "candidates": source.result["candidates"],
        "assumptions": source.result["assumptions"],
        "selected_id": "first",
        "source_synthetic": False,
        "source_recommendation_ready": True,
        "source_job_id": "original",
        "source_data_hash": "training-data",
    }


def test_period_comparison_has_no_reoptimization_or_compounded_returns(monkeypatch):
    request = request_data()
    before = copy.deepcopy(request)
    calls = []

    def simulate(spec, bars, **kwargs):
        calls.append((spec.model_dump(mode="json"), bars[0].at, kwargs))
        return {
            "profit": "20" if bars[0].at.month == 2 else "40",
            "return_pct": "2" if bars[0].at.month == 2 else "4",
            "max_drawdown": "5",
            "completed_cycles": 4,
            "halted": "",
        }

    monkeypatch.setattr(validation, "backtest", simulate)
    result = validation.revalidate(request, [segment(2), segment(3)])
    assert request == before and len(calls) == 4
    assert all(
        c[0] == candidate()["spec"] and c[2]["daily_loss"] == D(7) and c[2]["drawdown"] == D(13)
        for c in calls
    )
    first, second = result["candidates"]
    assert first["mean_return_pct"] == "3" and first["recommendation_ready"]
    assert not second["recommendation_ready"]
    assert first["observed_days"] == 40
    assert first["positive_periods"] == 2
    contaminated = validation.revalidate(request, [segment(2, "synthetic"), segment(3)])
    assert contaminated["synthetic"] and not contaminated["candidates"][0]["recommendation_ready"]
    with pytest.raises(validation.ResearchCancelled):
        validation.revalidate(request, [segment(2), segment(3)], stop_requested=lambda: True)


async def test_saved_evidence_is_immutable_and_draft_never_places_orders(db):
    _, sessions = db
    async with sessions.begin() as session:
        source = source_job()
        session.add(source)
    async with client_for(sessions) as client:
        data = {
            "name": "검증한 그리드",
            "job_id": source.id,
            "candidate_id": "first",
            "spec": {"budget": "5000"},
            "recommendation_ready": False,
        }
        response = await client.post("/api/recommendations", json=data)
        assert response.status_code == 201
        saved = response.json()
        assert saved["evidence"]["recommendation_ready"]
        assert saved["evidence"]["spec"]["budget"] == "1000"
        again = await client.post("/api/recommendations", json={**data, "name": "다른 이름"})
        assert again.json()["id"] == saved["id"] and again.json()["name"] == data["name"]
        other = await client.post("/api/recommendations", json={**data, "candidate_id": "second"})
        assert not other.json()["evidence"]["recommendation_ready"]
        async with sessions.begin() as session:
            changed = await session.get(BacktestJob, source.id)
            changed.result = {"changed": True}
        listed = (await client.get("/api/recommendations")).json()
        assert next(s for s in listed if s["id"] == saved["id"])["evidence"] == saved["evidence"]
        draft = (await client.post(f"/api/recommendations/{saved['id']}/draft", json={"mode": "live"})).json()
        assert draft["mode"] == "paper" and draft["status"] == "DRAFT"
        assert not draft["state"]["funded"]
        assert (await client.post(f"/api/recommendations/{saved['id']}/draft", json={})).json()[
            "id"
        ] == draft["id"]
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Strategy)) == 1
            assert await session.scalar(select(func.count()).select_from(Command)) == 0
            assert await session.scalar(select(func.count()).select_from(Intent)) == 0
            assert await session.scalar(select(func.count()).select_from(Recommendation)) == 2


async def test_worker_reads_each_period_and_only_past_warmup(db, monkeypatch):
    _, sessions = db
    async with sessions.begin() as session:
        source = source_job()
        session.add(source)
        for part in [segment(1), segment(2), segment(3)]:
            for bar in part["bars"]:
                session.add(
                    CandleRow(
                        symbol="TEST",
                        interval="1m",
                        timestamp=bar.at.replace(tzinfo=None),
                        data=bar.json(),
                        source="toss",
                    )
                )
    async with client_for(sessions) as client:
        response = await client.post(f"/api/backtests/{source.id}/revalidate", json=body())
        async with sessions() as session:
            job = await session.get(BacktestJob, response.json()["id"])
    calls = []

    def simulate(spec, bars, **kwargs):
        assert all(b.at < bars[0].at for b in kwargs["warmup"])
        assert kwargs["daily_loss"] == 7 and kwargs["drawdown"] == 13
        calls.append(bars[0].at.month)
        return {"profit": "20", "return_pct": "2", "max_drawdown": "5", "completed_cycles": 4, "halted": ""}

    monkeypatch.setattr(validation, "backtest", simulate)
    result = await compute_job(Settings(daily_loss_usd=999, drawdown_usd=999), sessions, job, AsyncMock())
    assert calls == [2, 3, 2, 3]
    assert result["sources"] == ["toss"] and result["validation_version"] == 1
