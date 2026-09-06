from datetime import UTC, datetime

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from cotrader.domain import D, StrategySpec
from cotrader.research import ResearchCancelled, backtest, dataset_hash, performance_failures, summarize


class ValidationPeriod(BaseModel):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def ordered(self):
        if self.start >= self.end:
            raise ValueError("검증 기간의 시작은 종료보다 빨라야 합니다")
        if self.end > datetime.now(UTC):
            raise ValueError("아직 지나지 않은 기간은 검증할 수 없습니다")
        return self


class RevalidationInput(BaseModel):
    candidate_ids: list[str] = Field(min_length=1, max_length=5)
    periods: list[ValidationPeriod] = Field(min_length=2, max_length=6)

    @model_validator(mode="after")
    def distinct(self):
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ValueError("같은 설정을 중복 선택할 수 없습니다")
        self.periods.sort(key=lambda p: p.start)
        if any(a.end > b.start for a, b in zip(self.periods, self.periods[1:], strict=False)):
            raise ValueError("재검증 기간은 서로 겹칠 수 없습니다")
        return self


def revalidate(request, segments, *, progress=None, stop_requested=None):
    candidates = request["candidates"]
    risk = request["assumptions"]
    total = len(candidates) * len(segments)
    results = []
    for candidate in candidates:
        spec = StrategySpec.model_validate(candidate["spec"])
        periods, reasons = [], []
        for index, segment in enumerate(segments):
            if stop_requested and stop_requested():
                raise ResearchCancelled()
            bars = segment["bars"]
            if len(bars) < 300:
                raise ValueError(f"재검증 기간 {index + 1}: 최소 300개의 1분봉이 필요합니다")
            if progress:
                completed = len(results) * len(segments) + index
                progress(
                    {
                        "stage": "고정 설정 기간별 검증",
                        "completed": completed,
                        "total": total,
                        "percent": int(completed / total * 100),
                    }
                )
            result = summarize(
                backtest(
                    spec,
                    bars,
                    warmup=segment["warmup"],
                    daily_loss=D(risk["daily_loss"]),
                    drawdown=D(risk["drawdown"]),
                    stop_requested=stop_requested,
                )
            )
            failures = performance_failures(result)
            if "synthetic" in segment["sources"]:
                failures.append("가상 데이터 포함")
            reasons.extend(f"기간 {index + 1}: {reason}" for reason in failures)
            periods.append(
                {
                    **result,
                    "requested_start": segment["start"],
                    "requested_end": segment["end"],
                    "sources": segment["sources"],
                    "failures": failures,
                }
            )
        observed_days = len({bar.at.date() for segment in segments for bar in segment["bars"]})
        if observed_days < 20:
            reasons.append("새 검증 데이터가 20일분 미만")
        if request["source_synthetic"]:
            reasons.append("원래 설정을 가상 데이터에서 탐색함")
        if candidate["id"] != request["selected_id"]:
            reasons.append("원래 선정한 설정이 아닌 비교 후보 — 채택하려면 새 기간의 추가 확인 필요")
        if not request["source_recommendation_ready"]:
            reasons.append("원래 탐색 결과의 추천 기준 미충족")
        results.append(
            {
                "id": candidate["id"],
                "spec": candidate["spec"],
                "periods": periods,
                "mean_return_pct": str(sum((D(p["return_pct"]) for p in periods), D(0)) / len(periods)),
                "worst_drawdown": str(max(D(p["max_drawdown"]) for p in periods)),
                "positive_periods": sum(D(p["profit"]) > 0 for p in periods),
                "passed_periods": sum(not p["failures"] for p in periods),
                "observed_days": observed_days,
                "recommendation_ready": not reasons,
                "recommendation_reasons": reasons,
            }
        )
    return {
        "validation_version": 1,
        "candidates": results,
        "selected_id": request["selected_id"],
        "source_job_id": request["source_job_id"],
        "source_data_hash": request["source_data_hash"],
        "assumptions": risk,
        "data_hash": dataset_hash([bar for segment in segments for bar in segment["bars"]]),
        "synthetic": request["source_synthetic"] or any("synthetic" in s["sources"] for s in segments),
        "limitations": [
            "기간별로 같은 예산과 빈 보유 상태로 시작합니다. 평균 수익률은 기간별 단순 평균이며 연속·복리 수익률이 아닙니다.",
            "원래 설정과 비용·손실 한도를 고정합니다. 앞선 데이터는 신호 준비에만 사용합니다.",
            "비교 결과를 본 뒤 다른 후보를 선택하면 그 기간도 선정에 사용된 것입니다. 같은 기간을 반복 검사해 독립 검증으로 세지 마세요.",
            "20일과 거래 횟수 기준은 최소 점검 기준이며 통계적 유의성이나 미래 수익을 보장하지 않습니다.",
        ],
    }
