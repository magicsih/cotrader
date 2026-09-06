import copy
import hashlib
import json
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from cotrader.auth import require_actor
from cotrader.domain import StrategySpec
from cotrader.models import BacktestJob, Event, Recommendation, Strategy
from cotrader.services import create_strategy
from cotrader.validation import RevalidationInput

Actor = Annotated[str, Depends(require_actor)]


class SaveRecommendation(BaseModel):
    job_id: str = Field(min_length=1, max_length=36)
    candidate_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=100)


def candidate_evidence(job, candidate_id):
    if job.status != "SUCCEEDED" or not (
        job.result.get("optimizer_version") or job.result.get("validation_version")
    ):
        raise ValueError("완료된 최적화·재검증 결과만 저장할 수 있습니다")
    result = job.result
    candidate = next((c for c in result["candidates"] if c["id"] == candidate_id), None)
    if not candidate:
        raise ValueError("결과에 없는 설정입니다")
    if result.get("validation_version"):
        ready, reasons = candidate["recommendation_ready"], candidate["recommendation_reasons"]
    else:
        ready = candidate_id == result["selected_id"] and result["recommendation_ready"]
        reasons = (
            result["recommendation_reasons"]
            if candidate_id == result["selected_id"]
            else ["최종 기간에서 확인하지 않은 비교 후보"]
        )
    evidence = {
        "spec": candidate["spec"],
        "candidate": candidate,
        "source_job_id": result.get("source_job_id", job.id),
        "source_data_hash": result.get("source_data_hash", result["data_hash"]),
        "data_hash": result["data_hash"],
        "assumptions": result["assumptions"],
        "selected_id": result["selected_id"],
        "recommendation_ready": ready,
        "recommendation_reasons": reasons,
        "synthetic": result.get("synthetic", False),
        "segments": result.get("segments", []),
        "holdout": result.get("holdout") if candidate_id == result["selected_id"] else None,
        "options": result.get("options"),
        "library": result.get("library"),
        "limitations": result.get("limitations", []),
    }
    evidence["fingerprint"] = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    return copy.deepcopy(evidence)


def research_routes(sessions, serialize):
    router = APIRouter()

    @router.post("/api/backtests/{job_id}/revalidate", status_code=202)
    async def submit_revalidation(job_id: str, body: RevalidationInput, actor: Actor):
        async with sessions.begin() as session:
            source = await session.get(BacktestJob, job_id)
            if not source:
                raise HTTPException(404, "원래 탐색 작업을 찾을 수 없습니다")
            if source.status != "SUCCEEDED" or not source.result.get("optimizer_version"):
                raise ValueError("완료된 최적화 결과에서 재검증을 시작하세요")
            boundary = datetime.fromisoformat(source.request["end"])
            if body.periods[0].start < boundary:
                raise ValueError("원래 탐색의 종료 시각 이후부터 재검증할 수 있습니다")
            by_id = {c["id"]: c for c in source.result["candidates"]}
            if any(key not in by_id for key in body.candidate_ids):
                raise ValueError("원래 탐색 결과에 없는 후보입니다")
            payload = {
                "action": "revalidate",
                "validation_version": 1,
                "spec": source.request["spec"],
                "source_job_id": source.id,
                "source_data_hash": source.result["data_hash"],
                "source_synthetic": source.result.get("synthetic", False),
                "source_recommendation_ready": source.result["recommendation_ready"],
                "selected_id": source.result["selected_id"],
                "assumptions": source.result["assumptions"],
                "candidates": [{"id": key, "spec": by_id[key]["spec"]} for key in body.candidate_ids],
                "periods": [p.model_dump(mode="json") for p in body.periods],
                "start": body.periods[0].start.isoformat(),
                "end": body.periods[-1].end.isoformat(),
            }
            row = BacktestJob(request=copy.deepcopy(payload))
            session.add(row)
            await session.flush()
            session.add(
                Event(
                    kind="audit",
                    message="고정 설정 기간별 재검증 요청",
                    data={"actor": actor, "job_id": row.id, "source_job_id": source.id},
                )
            )
            return serialize(row)

    @router.get("/api/recommendations")
    async def recommendations(_actor: Actor):
        async with sessions() as session:
            rows = (
                await session.scalars(
                    select(Recommendation).order_by(Recommendation.created_at.desc()).limit(100)
                )
            ).all()
            return [serialize(row) for row in rows]

    @router.post("/api/recommendations", status_code=201)
    async def save_recommendation(body: SaveRecommendation, actor: Actor):
        if not body.name.strip():
            raise ValueError("설정 이름을 입력하세요")
        async with sessions.begin() as session:
            job = await session.get(BacktestJob, body.job_id, with_for_update=True)
            if not job:
                raise HTTPException(404, "검증 작업을 찾을 수 없습니다")
            evidence = candidate_evidence(job, body.candidate_id)
            existing = await session.scalar(
                select(Recommendation).where(
                    Recommendation.job_id == job.id, Recommendation.candidate_id == body.candidate_id
                )
            )
            if existing:
                return serialize(existing)
            row = Recommendation(
                name=body.name.strip(), job_id=job.id, candidate_id=body.candidate_id, evidence=evidence
            )
            session.add(row)
            await session.flush()
            session.add(
                Event(
                    kind="audit",
                    message="검증 근거와 추천 설정 저장",
                    data={"actor": actor, "recommendation_id": row.id},
                )
            )
            return serialize(row)

    @router.post("/api/recommendations/{recommendation_id}/draft", status_code=201)
    async def recommendation_draft(recommendation_id: str, actor: Actor):
        async with sessions.begin() as session:
            saved = await session.get(Recommendation, recommendation_id, with_for_update=True)
            if not saved:
                raise HTTPException(404, "저장한 설정을 찾을 수 없습니다")
            if saved.strategy_id:
                existing = await session.get(Strategy, saved.strategy_id)
                if not existing:
                    raise HTTPException(409, "연결된 전략을 찾을 수 없습니다")
                return serialize(existing)
            strategy = await create_strategy(
                session, saved.name, StrategySpec.model_validate(saved.evidence["spec"]), "paper"
            )
            saved.strategy_id = strategy.id
            session.add(
                Event(
                    kind="audit",
                    message="저장 설정으로 모의 전략 초안 생성",
                    data={"actor": actor, "recommendation_id": saved.id, "strategy_id": strategy.id},
                )
            )
            return serialize(strategy)

    return router
