from datetime import timedelta
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from cotrader.auth import require_actor
from cotrader.discovery import ACTIVE_DISCOVERIES, LOOKBACK_DAYS, UNIVERSES
from cotrader.discovery_jobs import create_run, plan_request
from cotrader.domain import StrategySpec
from cotrader.markets import Venue
from cotrader.models import AccountState, Command, DiscoveryPlan, DiscoveryRun, Event, Strategy, now
from cotrader.services import create_strategy

Actor = Annotated[str, Depends(require_actor)]


class DiscoveryInput(BaseModel):
    venue: Venue
    budget: Decimal = Field(gt=0, le=10000000)
    preference: Literal["balanced", "drawdown"] = "drawdown"
    frequency: Literal["once", "daily"] = "once"


def discovery_routes(settings, sessions, serialize):
    router = APIRouter()

    @router.get("/api/discoveries")
    async def list_runs(_actor: Actor, venue: Venue = "toss"):
        async with sessions() as session:
            plan = await session.get(DiscoveryPlan, venue)
            rows = (
                await session.scalars(
                    select(DiscoveryRun)
                    .where(DiscoveryRun.venue == venue)
                    .order_by(DiscoveryRun.created_at.desc())
                    .limit(20)
                )
            ).all()
            return {
                "plan": serialize(plan) if plan else None,
                "runs": [serialize(r) for r in rows],
                "defaults": {
                    "universe": UNIVERSES[venue],
                    "lookback_days": LOOKBACK_DAYS,
                    "budget": str(
                        min(settings.capital_for(venue), Decimal(10000000 if venue == "upbit" else 5000))
                    ),
                    "risk": settings.risk_for(venue),
                },
            }

    @router.post("/api/discoveries", status_code=202)
    async def start(body: DiscoveryInput, actor: Actor):
        if (body.venue == "upbit" and not settings.upbit_enabled) or (
            body.venue == "toss" and settings.market_source != "toss"
        ):
            raise ValueError("선택한 시장의 시세 연결이 비활성입니다")
        if body.budget > min(
            settings.capital_for(body.venue), Decimal(10000000 if body.venue == "upbit" else 5000)
        ):
            raise ValueError("서버의 시장별 모의 예산 한도를 넘을 수 없습니다")
        request = plan_request(settings, body.venue, body.budget, body.preference)
        async with sessions.begin() as session:
            account = await session.get(
                AccountState, {"venue": body.venue, "mode": "paper"}, with_for_update=True
            )
            if not account:
                raise ValueError("실행기가 계좌 기준을 준비한 뒤 다시 요청하세요")
            plan = await session.get(DiscoveryPlan, body.venue, with_for_update=True)
            if plan is None:
                plan = DiscoveryPlan(venue=body.venue, request=request)
                session.add(plan)
            active = await session.scalar(
                select(DiscoveryRun).where(
                    DiscoveryRun.venue == body.venue, DiscoveryRun.status.in_(ACTIVE_DISCOVERIES)
                )
            )
            if active:
                raise HTTPException(409, "이 시장의 탐색이 이미 진행 중입니다. 결과를 기다리거나 중단하세요")
            plan.request, plan.enabled = request, body.frequency == "daily"
            plan.next_run_at = now() + timedelta(days=1) if plan.enabled else None
            return serialize(await create_run(session, request, actor))

    @router.post("/api/discoveries/schedule/stop")
    async def stop_schedule(actor: Actor, venue: Venue = "toss"):
        async with sessions.begin() as session:
            plan = await session.get(DiscoveryPlan, venue, with_for_update=True)
            if plan:
                plan.enabled, plan.next_run_at = False, None
                session.add(
                    Event(kind="audit", message="매일 전략 탐색 중지", data={"actor": actor, "venue": venue})
                )
            return {"ok": True}

    @router.post("/api/discoveries/{run_id}/cancel")
    async def cancel(run_id: str, actor: Actor):
        async with sessions.begin() as session:
            row = await session.get(DiscoveryRun, run_id, with_for_update=True)
            if not row:
                raise HTTPException(404, "탐색을 찾을 수 없습니다")
            if row.status == "COLLECTING":
                for cid in row.request["commands"].values():
                    c = await session.get(Command, cid, with_for_update=True)
                    if c and c.status in {"QUEUED", "RUNNING"}:
                        c.status = "CANCELED"
                row.status, row.result = "CANCELED", {"message": "시세 수집과 탐색을 중단했습니다"}
            elif row.status == "RUNNING":
                row.status = "CANCEL_REQUESTED"
            session.add(
                Event(
                    kind="audit", message="전략 탐색 중단 요청", data={"actor": actor, "discovery_id": row.id}
                )
            )
            return serialize(row)

    @router.post("/api/discoveries/{run_id}/draft", status_code=201)
    async def draft(run_id: str, actor: Actor):
        async with sessions.begin() as session:
            row = await session.get(DiscoveryRun, run_id, with_for_update=True)
            if not row or row.status != "SUCCEEDED" or not row.result.get("selected"):
                raise ValueError("완료된 탐색의 후보가 필요합니다")
            if row.strategy_id:
                existing = await session.get(Strategy, row.strategy_id)
                if not existing:
                    raise HTTPException(409, "저장한 전략을 찾을 수 없습니다")
                return serialize(existing)
            spec = StrategySpec.model_validate(row.result["selected"]["spec"])
            strategy = await create_strategy(session, f"{spec.symbol} · 자동 탐색 모의 초안", spec, "paper")
            row.strategy_id = strategy.id
            session.add(
                Event(
                    kind="audit",
                    strategy_id=strategy.id,
                    message="탐색 근거에서 모의 초안 저장",
                    data={"actor": actor, "discovery_id": row.id, "fingerprint": row.result["fingerprint"]},
                )
            )
            return serialize(strategy)

    return router
