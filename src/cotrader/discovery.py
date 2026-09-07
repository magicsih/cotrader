"""Bounded, cross-symbol selection with one untouched final evaluation."""

import hashlib
import json
from datetime import datetime, timedelta

from cotrader.domain import D, StrategySpec
from cotrader.research import (
    OptimizationOptions,
    ResearchCancelled,
    backtest,
    dataset_hash,
    optimize_grid,
    performance_failures,
    summarize,
)

UNIVERSES = {
    "toss": [{"symbol": "SPY", "name": "미국 S&P 500 ETF"}, {"symbol": "QQQ", "name": "나스닥 100 ETF"}],
    "upbit_usdt": [{"symbol": "USDT-BTC", "name": "비트코인"}, {"symbol": "USDT-ETH", "name": "이더리움"}],
    "upbit": [{"symbol": "KRW-BTC", "name": "비트코인"}, {"symbol": "KRW-ETH", "name": "이더리움"}],
}
ACTIVE_DISCOVERIES = ("COLLECTING", "RUNNING", "CANCEL_REQUESTED")
LOOKBACK_DAYS = 30
KIND_NAMES = {"grid": "그리드", "trend": "추세 추종", "rebound": "과매도 반등"}


def candidate_id(spec):
    return hashlib.sha256(spec.model_dump_json().encode()).hexdigest()[:20]


def ranking(candidate, weight):
    # Never inspect final-period performance when ranking symbols or strategies.
    result = candidate["validation"]
    score = D(result["profit"]) - D(str(weight)) * D(result["max_drawdown"])
    return bool(candidate["selection_failures"]), -score, D(result["max_drawdown"]), candidate["id"]


def discover(request, datasets, *, progress=None, stop_requested=None):
    weight = 2 if request["preference"] == "drawdown" else 1
    risk = request["risk"]
    args = {
        "daily_loss": D(risk["daily_loss"]),
        "drawdown": D(risk["drawdown"]),
        "stop_requested": stop_requested,
    }
    candidates, excluded, usable = [], [], {}
    total = len(request["symbols"])
    datasets = {
        symbol: {**data, "bars": sorted(data.get("bars", []), key=lambda b: b.at)}
        for symbol, data in datasets.items()
    }
    comparable = [
        data
        for data in datasets.values()
        if len(data["bars"]) >= 300 and "synthetic" not in data.get("sources", [])
    ]
    if len(comparable) > 1:
        # Identical timestamps keep every symbol's training/validation/final boundary aligned.
        common = set.intersection(*[{b.at for b in data["bars"]} for data in comparable])
        for data in comparable:
            data["bars"] = [b for b in data["bars"] if b.at in common]

    def report(stage, percent):
        if stop_requested and stop_requested():
            raise ResearchCancelled()
        if progress:
            progress({"stage": stage, "percent": percent})

    for index, symbol in enumerate(request["symbols"]):
        report(f"{symbol} 데이터 확인", int(index * 90 / total))
        dataset = datasets.get(symbol, {})
        bars = sorted(dataset.get("bars", []), key=lambda b: b.at)
        if len(bars) < 300 or "synthetic" in dataset.get("sources", []):
            excluded.append(
                {
                    "symbol": symbol,
                    "reason": dataset.get("error") or "실제 1분봉이 300개 미만이거나 가상 데이터가 포함됨",
                }
            )
            continue
        cut, final_cut = len(bars) * 3 // 5, len(bars) * 4 // 5
        train, validation = bars[:cut], bars[cut:final_cut]
        usable[symbol] = bars
        base = StrategySpec(
            venue=request["venue"],
            symbol=symbol,
            kind="trend",
            budget=request["budget"],
            commission_rate=request["commission_rate"],
            slippage_bps=request["slippage_bps"],
        )
        try:
            grid = optimize_grid(
                base,
                bars,
                final_check=False,
                options=OptimizationOptions(method="tpe", trials=24, seed=42, drawdown_weight=weight),
                progress=lambda p, symbol=symbol, index=index: report(
                    f"{symbol} 그리드 · {p['stage']}", int((index + p["percent"] / 100 * 0.65) * 90 / total)
                ),
                **args,
            )
            for c in grid["candidates"]:
                candidates.append({**c, "id": candidate_id(StrategySpec.model_validate(c["spec"]))})
        except ValueError as exc:
            excluded.append({"symbol": symbol, "kind": "grid", "reason": str(exc)})
        variations = [
            {"kind": "trend", "timeframe": timeframe, "fast": fast, "slow": slow}
            for timeframe in (15, 30)
            for fast, slow in ((5, 20), (10, 40))
        ] + [
            {
                "kind": "rebound",
                "timeframe": timeframe,
                "fast": 10,
                "slow": 40,
                "rsi_period": period,
                "rsi_entry": entry,
                "rsi_exit": 55,
            }
            for timeframe in (15, 30)
            for period, entry in ((14, 30), (7, 25))
        ]
        for number, change in enumerate(variations):
            report(
                f"{symbol} {KIND_NAMES[change['kind']]} 비교",
                int((index + 0.65 + number / len(variations) * 0.3) * 90 / total),
            )
            spec = StrategySpec.model_validate({**base.model_dump(), **change})
            training = summarize(backtest(spec, train, **args))
            validation_result = summarize(backtest(spec, validation, warmup=train, **args))
            candidates.append(
                {
                    "id": candidate_id(spec),
                    "spec": spec.model_dump(mode="json"),
                    "training": training,
                    "validation": validation_result,
                    "selection_failures": [f"탐색 구간: {r}" for r in performance_failures(training)]
                    + [f"중간 구간: {r}" for r in performance_failures(validation_result)],
                }
            )
    if not candidates:
        raise ValueError(
            "비교할 실제 데이터가 없습니다. " + " · ".join(f"{e['symbol']}: {e['reason']}" for e in excluded)
        )
    ordered = sorted(candidates, key=lambda c: ranking(c, weight))
    selected = ordered[0]
    # Freeze the winner across all symbols and all families before testing any final period.
    report("선정한 한 가지 설정만 마지막 기간 확인", 95)
    spec = StrategySpec.model_validate(selected["spec"])
    bars = usable[spec.symbol]
    final_cut = len(bars) * 4 // 5
    final = summarize(backtest(spec, bars[final_cut:], warmup=bars[:final_cut], **args))
    reasons = list(selected["selection_failures"])
    reasons += [f"마지막 구간: {r}" for r in performance_failures(final)]
    if len({b.at.date() for b in bars}) < 20:
        reasons.append("실제 관측일이 20일 미만")
    if len(usable) < total:
        reasons.append("예정한 종목 중 일부의 실제 데이터가 부족함")
    if bars[-1].at < datetime.fromisoformat(request["end"]) - timedelta(
        days=4 if spec.venue == "toss" else 1
    ):
        reasons.append("마지막 시세가 요청한 종료일에 비해 오래됨")
    if spec.kind == "grid" and not spec.lower <= bars[-1].close <= spec.upper:
        reasons.append("마지막 관측 가격이 그리드 범위를 벗어남")
    report("탐색 완료", 100)
    result = {
        "discovery_version": 1,
        "selected": {**selected, "final": final},
        "recommendation_ready": not reasons,
        "recommendation_reasons": reasons,
        "compared_count": len(candidates),
        "candidates": ordered[:10],
        "excluded": excluded,
        "selection_rule": f"앞 60%와 중간 20%의 성과 조건을 통과한 후보를 우선하고 중간 구간 손익 − 최대 하락폭 × {weight}로 선정. 마지막 20%는 선정한 한 설정만 확인.",
        "datasets": [
            {
                "symbol": symbol,
                "count": len(series),
                "first": series[0].at.isoformat(),
                "last": series[-1].at.isoformat(),
                "hash": dataset_hash(series),
                "sources": datasets[symbol]["sources"],
            }
            for symbol, series in usable.items()
        ],
        "assumptions": {
            **risk,
            "budget": request["budget"],
            "commission_rate": request["commission_rate"],
            "slippage_bps": request["slippage_bps"],
        },
        "limitations": [
            "후보 사이에 공통으로 존재하는 시각의 봉만 비교합니다. 미리 정한 시작용 후보 종목 안에서 비교한 결과입니다. 전체 시장의 최고 수익 종목을 찾은 것이 아닙니다.",
            "매일 탐색하면 이전 30일 구간과 대부분 겹칩니다. 반복 결과를 독립 검증 횟수로 합산하지 않습니다.",
            "기간별 예산은 동일한 가상 자금이며 실제 자산은 사용하지 않습니다. 기존 전략과 실제 주문은 변경하지 않습니다.",
            "기업행사·환율·세금·실제 호가 대기열과 과거 호가 단위 변경은 반영하지 않습니다.",
        ],
    }
    result["fingerprint"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result


def window_end(at):
    return at.replace(hour=0, minute=0, second=0, microsecond=0)


def research_window(at):
    end = window_end(at)
    return end - timedelta(days=LOOKBACK_DAYS), end
