import hashlib
import json
from bisect import bisect_right
from datetime import timedelta
from typing import Literal

import optuna
from pydantic import BaseModel, Field

from cotrader.domain import (
    Bar,
    D,
    Decision,
    Quote,
    StrategySpec,
    aggregate,
    apply_fill,
    decide,
    equity,
    initial_state,
    tick,
)
from cotrader.markets import currency, round_quantity

DEFAULT_DAILY_LOSS = D("50")
DEFAULT_DRAWDOWN = D("250")


class ResearchCancelled(Exception):
    pass


def dataset_hash(bars):
    return hashlib.sha256(json.dumps([b.json() for b in bars], sort_keys=True).encode()).hexdigest()


def backtest(
    spec: StrategySpec,
    bars: list[Bar],
    *,
    daily_loss=DEFAULT_DAILY_LOSS,
    drawdown=DEFAULT_DRAWDOWN,
    warmup=None,
    stop_requested=None,
) -> dict:
    if spec.kind == "rotation":
        raise ValueError("ETF 교체 전략은 7개 ETF의 배당 포함 일봉으로 함께 검증해야 합니다")
    if spec.inventory_quantity:
        raise ValueError(
            "보유 코인 편입 초안은 현재 계좌와 평가 기준가를 포함합니다. 과거 검증에는 별도의 현금 기준 연구 설정을 사용하세요"
        )
    if len(bars) < 3:
        raise ValueError("백테스트에는 최소 3개의 1분봉이 필요합니다")
    bars = sorted(bars, key=lambda b: b.at)
    signal_history = max(spec.slow, spec.rsi_period) + 10
    warmup = sorted(warmup or [], key=lambda b: b.at)[-spec.timeframe * signal_history :]
    if warmup and warmup[-1].at >= bars[0].at:
        raise ValueError("신호 준비 데이터는 검증 구간보다 과거여야 합니다")
    if len({b.at for b in bars}) != len(bars):
        raise ValueError("중복 봉이 있습니다")
    for bar in [*warmup, *bars]:
        if (
            not (0 < bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high)
            or bar.volume < 0
        ):
            raise ValueError("OHLCV 값이 유효하지 않습니다")
    state = initial_state(spec.budget)
    pending: Decision | None = None
    trades, curve = [], []
    high, day_anchor, max_dd = spec.budget, spec.budget, D(0)
    halted, day = "", None
    gaps, completed_cycles = 0, 0
    # Precompute complete buckets, then reveal only buckets closed at each simulated instant.
    signals = (
        aggregate([*warmup, *bars], spec.timeframe, bars[-1].at + timedelta(minutes=1))
        if spec.kind != "grid" or spec.signal_gate
        else []
    )
    signal_ends = [b.at + timedelta(minutes=spec.timeframe) for b in signals]
    for index, bar in enumerate(bars):
        if index % 256 == 0 and stop_requested and stop_requested():
            raise ResearchCancelled()
        previous = bars[index - 1] if index else bar
        if index and (bar.at - previous.at).total_seconds() != 60:
            gaps += 1
            pending = None
            state["last_mid"] = None
        # A decision formed at the previous close can only fill in a later bar.
        if pending and not halted:
            crossed = bar.low < pending.price if pending.side == "BUY" else bar.high > pending.price
            cap = round_quantity(bar.volume * D("0.01"), spec.venue)
            quantity = min(pending.quantity, cap)
            if crossed and quantity > 0:
                # Do not invent price improvement. Limit price bounds every fill.
                price = pending.price
                amount = quantity * price
                costs = amount * spec.commission_rate
                if pending.side == "SELL" or D(state["cash"]) >= amount + costs:
                    apply_fill(state, pending.side, quantity, amount, costs, pending.slot)
                    if (
                        pending.side == "SELL"
                        and (
                            D(state["lots"][str(pending.slot)]["quantity"])
                            if pending.slot is not None
                            else D(state["quantity"])
                        )
                        == 0
                    ):
                        completed_cycles += 1
                    trades.append(
                        {
                            "at": bar.at.isoformat(),
                            "side": pending.side,
                            "quantity": str(quantity),
                            "price": str(price),
                            "costs": str(costs),
                            "reason": pending.reason,
                        }
                    )
            pending = None
        value = equity(state, bar.close)
        local_day = bar.at.date().isoformat()
        if local_day != day:
            day, day_anchor = local_day, value
        high, max_dd = max(high, value), max(max_dd, high - value)
        if day_anchor - value >= daily_loss or high - value >= drawdown:
            halted = "손실 기준 도달 — 보유 유지"
        at = bar.at + timedelta(minutes=1)
        spread = spec.slippage_bps / D(10000)
        quote = Quote(
            spec.symbol, bar.close * (1 - spread), bar.close * (1 + spread), at, bar.volume, bar.volume
        )
        if not halted and quote.valid(spec, at):
            visible = bisect_right(signal_ends, at)
            decision, reason = decide(
                spec,
                state,
                quote,
                signals[max(0, visible - signal_history) : visible],
            )
            if reason == "GRID_LOWER_BREACH":
                halted = "그리드 하단 이탈 — 보유 유지"
            elif decision and (
                decision.side == "SELL"
                or D(state["cash"]) >= decision.price * decision.quantity * (1 + spec.commission_rate)
            ):
                pending = decision
        if index % max(1, len(bars) // 500) == 0 or index == len(bars) - 1:
            curve.append({"at": bar.at.isoformat(), "equity": str(value)})
    final = equity(state, bars[-1].close)
    hold_qty = round_quantity(spec.budget / (bars[0].open * (1 + spec.commission_rate)), spec.venue)
    benchmark = spec.budget - hold_qty * bars[0].open * (1 + spec.commission_rate) + hold_qty * bars[-1].close
    digest = dataset_hash(bars)
    return {
        "equity": str(final),
        "profit": str(final - spec.budget),
        "return_pct": str((final / spec.budget - 1) * 100),
        "max_drawdown": str(max_dd),
        "realized_gross": state["realized"],
        "costs": state["costs"],
        "held_quantity": state["quantity"],
        "unrealized": str(D(state["quantity"]) * bars[-1].close - D(state["cost_basis"])),
        "buy_hold_equity": str(benchmark),
        "trade_count": len(trades),
        "completed_cycles": completed_cycles,
        "trades": trades,
        "curve": curve,
        "halted": halted,
        "data_hash": digest,
        "warmup_hash": dataset_hash(warmup),
        "first": bars[0].at.isoformat(),
        "last": bars[-1].at.isoformat(),
        "bar_count": len(bars),
        "gaps": gaps,
        "config": spec.model_dump(mode="json"),
        "venue": spec.venue,
        "currency": currency(spec.venue),
        "limitations": [
            "1분봉 기반 추정: 같은 봉 안의 연쇄 매수·매도는 실행하지 않습니다.",
            "현재 거래 단위를 사용하며 과거 호가 정책 변경을 재현하지 않습니다.",
            "가격선 단순 접촉은 미체결, 거래량의 1%까지만 체결로 가정합니다.",
            "스프레드는 설정한 편도 체결 비용으로 가정합니다. 실제 호가·대기 순서는 재현하지 못합니다.",
            "수수료는 설정값이며 배당·분할·환율·양도소득세를 반영하지 않습니다.",
            "데이터의 공백이 휴장인지 유실인지는 별도 검증이 필요합니다. 이 결과만으로 실거래 적합성을 확정하지 않습니다.",
        ],
    }


class OptimizationOptions(BaseModel):
    method: Literal["exhaustive", "random", "tpe", "nsga2", "compare"] = "exhaustive"
    space: Literal["compact", "wide"] = "compact"
    trials: int = Field(default=100, ge=20, le=1000)
    seed: int = Field(default=42, ge=0, le=2**32 - 1)
    drawdown_weight: float = Field(default=0, ge=0, le=5)


def grid_axes(training, expanded=False):
    prices = sorted(b.close for b in training)
    low, high = min(b.low for b in training), max(b.high for b in training)
    ranges = [
        ("중앙 가격 범위", prices[len(prices) // 10], prices[len(prices) * 9 // 10]),
        ("관측 가격 범위", low, high),
        ("상하 5% 여유", low * D("0.95"), high * D("1.05")),
    ]
    gates = [dict(signal_gate=False, timeframe=15, fast=20, slow=60)]
    gates += [
        dict(signal_gate=True, timeframe=frame, fast=fast, slow=slow)
        for frame in (5, 15, 30)
        for fast, slow in ((5, 20), (10, 40))
    ]
    return ranges, gates, list(range(3, 21)) if expanded else [3, 5, 8, 12]


def grid_candidate(base, axes, params):
    ranges, gates, _ = axes
    label, lower, upper = ranges[params["range"]]
    spec = StrategySpec(
        **{
            **base.model_dump(),
            "kind": "grid",
            "lower": tick(lower, base.venue),
            "upper": tick(upper, base.venue),
            "grids": params["grids"],
            "spacing": params["spacing"],
            **gates[params["gate"]],
        }
    )
    key = hashlib.sha256(spec.model_dump_json().encode()).hexdigest()[:16]
    return key, label, spec


def grid_search_space(base: StrategySpec, training: list[Bar], expanded=False):
    axes = grid_axes(training, expanded)
    candidates, seen, invalid = [], set(), 0
    for index in range(len(axes[0])):
        for count in axes[2]:
            for spacing in ("arithmetic", "geometric"):
                for gate in range(len(axes[1])):
                    try:
                        item = grid_candidate(
                            base, axes, dict(range=index, grids=count, spacing=spacing, gate=gate)
                        )
                    except ValueError:
                        invalid += 1
                        continue
                    if item[0] not in seen:
                        candidates.append(item)
                        seen.add(item[0])
    return candidates, invalid


def summarize(result):
    return {k: v for k, v in result.items() if k not in {"curve", "trades", "config", "limitations"}}


def performance_failures(result):
    failures = []
    if D(result["profit"]) <= 0:
        failures.append("비용 후 총손익이 양수가 아님")
    if result["completed_cycles"] < 3:
        failures.append("완료된 매수·매도 묶음이 3회 미만")
    if result["halted"]:
        failures.append("운영 중단 기준에 도달함")
    return failures


def optimize_grid(
    base: StrategySpec,
    bars: list[Bar],
    *,
    daily_loss=DEFAULT_DAILY_LOSS,
    drawdown=DEFAULT_DRAWDOWN,
    synthetic=False,
    progress=None,
    stop_requested=None,
    options: OptimizationOptions | None = None,
    final_check: bool = True,
) -> dict:
    options = options or OptimizationOptions()
    if len(bars) < 300:
        raise ValueError("설정 탐색에는 최소 300개의 1분봉이 필요합니다")
    if base.slippage_bps * 2 > base.max_spread_bps:
        raise ValueError("편도 호가 비용의 두 배가 호가 차이 상한을 넘습니다. 비용 가정과 상한을 확인하세요")
    ordered = sorted(bars, key=lambda b: b.at)
    cut, final_cut = len(ordered) * 3 // 5, len(ordered) * 4 // 5
    train, validation, holdout = ordered[:cut], ordered[cut:final_cut], ordered[final_cut:]
    space, invalid = grid_search_space(base, train, options.space == "wide")
    axes = grid_axes(train, options.space == "wide")
    if not space:
        raise ValueError("현재 예산·변동 범위에서 최소 주문 및 비용 조건을 만족하는 그리드가 없습니다")

    def report(stage, completed, total, percent):
        if stop_requested and stop_requested():
            raise ResearchCancelled()
        if progress:
            progress({"stage": stage, "completed": completed, "total": total, "percent": percent})

    def run(spec, segment, warmup=None):
        return summarize(
            backtest(
                spec,
                segment,
                warmup=warmup,
                daily_loss=daily_loss,
                drawdown=drawdown,
                stop_requested=stop_requested,
            )
        )

    def score(result):
        return D(result["profit"]) - D(str(options.drawdown_weight)) * D(result["max_drawdown"])

    cache, searches = {}, []
    methods = ["random", "tpe", "nsga2"] if options.method == "compare" else [options.method]
    for method_index, method in enumerate(methods):
        visited, attempts, rejected, duplicates = set(), 0, 0, 0
        history = []
        total = len(space) if method == "exhaustive" else options.trials
        study = None
        if method != "exhaustive":
            sampler = {
                "random": lambda: optuna.samplers.RandomSampler(seed=options.seed),
                "tpe": lambda: optuna.samplers.TPESampler(seed=options.seed, n_startup_trials=10),
                "nsga2": lambda: optuna.samplers.NSGAIISampler(seed=options.seed, population_size=10),
            }[method]()
            optuna.logging.set_verbosity(optuna.logging.WARNING)
            study = optuna.create_study(directions=["maximize", "minimize"], sampler=sampler)
            # Give every method the same valid starting point, even when most combinations are invalid.
            _, label, first = space[0]
            study.enqueue_trial(
                {
                    "range": next(i for i, r in enumerate(axes[0]) if r[0] == label),
                    "grids": first.grids,
                    "spacing": first.spacing,
                    "gate": next(
                        i for i, g in enumerate(axes[1]) if all(getattr(first, k) == v for k, v in g.items())
                    ),
                }
            )
        for index in range(total):
            report(
                f"{method} 설정 탐색", index, total, int((method_index + index / total) / len(methods) * 80)
            )
            attempts += 1
            trial = study.ask() if study else None
            try:
                item = (
                    grid_candidate(
                        base,
                        axes,
                        {
                            "range": trial.suggest_int("range", 0, len(axes[0]) - 1),
                            "grids": trial.suggest_categorical("grids", axes[2]),
                            "spacing": trial.suggest_categorical("spacing", ["arithmetic", "geometric"]),
                            "gate": trial.suggest_int("gate", 0, len(axes[1]) - 1),
                        },
                    )
                    if trial
                    else space[index]
                )
            except ValueError:
                rejected += 1
                # A deterministic dominated score keeps infeasible trials out of the genetic population.
                study.tell(trial, [-float(base.budget) * 100, float(base.budget) * 100])
                continue
            key, label, spec = item
            duplicates += int(key in visited)
            visited.add(key)
            if key not in cache:
                fit = run(spec, train)
                cache[key] = {
                    "id": key,
                    "range_label": label,
                    "spec": spec.model_dump(mode="json"),
                    "training_profit": fit["profit"],
                    "training": fit,
                    "training_score": str(score(fit)),
                }
            fit = cache[key]["training"]
            if trial:
                study.tell(trial, [float(fit["profit"]), float(fit["max_drawdown"])])
            best = min((cache[k] for k in visited), key=lambda c: (-score(c["training"]), c["id"]))
            history.append(
                {"trial": attempts, "score": best["training_score"], "profit": best["training_profit"]}
            )
        best = min(
            (cache[k] for k in visited),
            key=lambda c: (-score(c["training"]), D(c["training"]["max_drawdown"]), c["id"]),
        )
        searches.append(
            {
                "method": method,
                "attempts": attempts,
                "unique_count": len(visited),
                "invalid_attempts": rejected,
                "duplicate_attempts": duplicates,
                "best_id": best["id"],
                "best_profit": best["training_profit"],
                "best_drawdown": best["training"]["max_drawdown"],
                "best_score": best["training_score"],
                "history": history,
            }
        )
    candidates = sorted(
        cache.values(), key=lambda c: (-score(c["training"]), D(c["training"]["max_drawdown"]), c["id"])
    )
    finalists = candidates[:5]
    for index, candidate in enumerate(finalists):
        report("중간 기간 비교", index, len(finalists), 80 + int(index / len(finalists) * 15))
        candidate["validation"] = run(StrategySpec.model_validate(candidate["spec"]), validation, train)
        candidate["selection_failures"] = [
            f"탐색 구간: {r}" for r in performance_failures(candidate["training"])
        ]
        candidate["selection_failures"] += [
            f"중간 구간: {r}" for r in performance_failures(candidate["validation"])
        ]
        candidate["note"] = "탐색 구간 점수 상위 5개. 중간 20%에서 선정한 1개만 마지막 20%로 확인합니다."
    eligible = [c for c in finalists if not c["selection_failures"]]
    selected = (
        min(
            eligible,
            key=lambda c: (-score(c["validation"]), D(c["validation"]["max_drawdown"]), c["id"]),
        )
        if eligible
        else finalists[0]
    )
    # This selection is frozen before reading the last segment's prices or performance.
    report("마지막 기간 최종 확인", 0, 1, 95)
    final_result = (
        run(StrategySpec.model_validate(selected["spec"]), holdout, ordered[:final_cut])
        if final_check
        else None
    )
    reasons = list(selected["selection_failures"])
    reasons += (
        [f"마지막 구간: {r}" for r in performance_failures(final_result)]
        if final_check
        else ["종목 간 선정 후 최종 기간 확인 대기"]
    )
    observed_days = len({b.at.date() for b in ordered})
    if observed_days < 20:
        reasons.append("전체 데이터가 20일분 미만")
    if synthetic:
        reasons.append("가상 데이터이므로 실제 운용 설정으로 추천하지 않음")
    report("완료", len(space), len(space), 100)
    return {
        "optimizer_version": 2,
        "options": options.model_dump(),
        "library": {"name": "optuna", "version": optuna.__version__},
        "searches": searches,
        "objective": f"비용 후 총손익 - {options.drawdown_weight:g} × 최대 하락폭",
        "tested_count": len(cache),
        "valid_space_count": len(space),
        "invalid_count": invalid,
        "possible_count": len(axes[0]) * len(axes[1]) * len(axes[2]) * 2,
        "candidates": finalists,
        "training_winner_id": finalists[0]["id"],
        "selected_id": selected["id"],
        "holdout": final_result,
        "recommendation_ready": not reasons,
        "recommendation_reasons": reasons,
        "synthetic": synthetic,
        "observed_days": observed_days,
        "data_hash": dataset_hash(ordered),
        "assumptions": {
            "daily_loss": str(daily_loss),
            "drawdown": str(drawdown),
            "commission_rate": str(base.commission_rate),
            "slippage_bps": str(base.slippage_bps),
        },
        "segments": [
            {
                "name": name,
                "count": len(segment),
                "first": segment[0].at.isoformat(),
                "last": segment[-1].at.isoformat(),
            }
            for name, segment in (
                ("설정 탐색 60%", train),
                ("후보 선정 20%", validation),
                ("최종 확인 20%", holdout),
            )
        ],
        "limitations": [
            "탐색한 범위와 기간에서의 순위이며 미래 수익의 최대값은 아닙니다.",
            "확률적 탐색은 전역 최적값을 보장하지 않습니다. 비교 모드도 모든 기법의 후보를 합친 뒤 최종 기간은 한 번만 씁니다.",
            "각 구간은 같은 예산·빈 보유 상태에서 독립 실행하며 앞 구간은 신호 준비에만 씁니다.",
            "기간을 바꾸어 반복 탐색하면 마지막 구간도 사실상 탐색에 쓰이므로 새 기간의 검증이 필요합니다.",
            "분할·배당·환율·양도소득세·실제 호가 대기열을 반영하지 않습니다.",
        ],
    }
