"""Fixed monthly ETF policy, with a shared cash ledger and serial orders."""

from zoneinfo import ZoneInfo

from cotrader.domain import ETF_UNIVERSE, D, Decision, StrategySpec, apply_fill, initial_state, tick
from cotrader.markets import order_size_valid, round_quantity

NEW_YORK = ZoneInfo("America/New_York")
LOOKBACK = 252
TOP = 2
REBALANCE_BAND = D("0.05")


def specification(budget="5000") -> StrategySpec:
    return StrategySpec(
        venue="toss",
        symbol="ETF-ROTATION",
        kind="rotation",
        budget=budget,
        commission_rate=D("0.001"),
        slippage_bps=D("5"),
        max_spread_bps=D("30"),
    )


def held(state, symbol):
    return D(state.get("positions", {}).get(symbol, {}).get("quantity", "0"))


def apply_execution(state, symbol, side, quantity, amount, costs, universe=ETF_UNIVERSE):
    if symbol not in universe:
        raise ValueError("ETF 교체 전략의 투자 대상과 체결 종목이 다릅니다")
    positions = {key: dict(value) for key, value in state.get("positions", {}).items()}
    position = {**initial_state(D(0)), **positions.get(symbol, {}), "cash": state["cash"]}
    apply_fill(position, side, quantity, amount, costs, None)
    positions[symbol] = {key: position[key] for key in ("quantity", "cost_basis", "realized", "costs")}
    state.update(cash=position["cash"], positions=positions)
    for key in ("quantity", "cost_basis", "realized", "costs"):
        state[key] = str(sum((D(p[key]) for p in positions.values()), D(0)))


def monthly_targets(series, signal_day):
    """Use only complete, aligned sessions; never silently shrink the universe."""
    if set(series) != set(ETF_UNIVERSE):
        raise ValueError("7개 ETF의 배당 포함 일봉이 모두 필요합니다")
    dates = [row["date"] for row in series[ETF_UNIVERSE[0]] if row["date"] <= signal_day]
    if len(dates) < LOOKBACK + 1 or dates != sorted(set(dates)):
        raise ValueError("중복 없는 완성 일봉 253개 이상이 필요합니다")
    scores = {}
    for symbol in ETF_UNIVERSE:
        rows = [r for r in series[symbol] if r["date"] <= signal_day]
        if [r["date"] for r in rows] != dates:
            raise ValueError("ETF별 거래일이 다릅니다. 누락 자료를 먼저 확인하세요")
        values = [D(str(r["total_return"])) for r in rows]
        if any(not value.is_finite() or value <= 0 for value in values):
            raise ValueError("배당 포함 수익 지수가 유효하지 않습니다")
        scores[symbol] = values[-1] / values[-LOOKBACK - 1] - 1
    ranked = sorted(ETF_UNIVERSE, key=lambda symbol: -scores[symbol])
    targets = {symbol: "0.5" for symbol in ranked[:TOP] if scores[symbol] > 0}
    return targets, {symbol: str(scores[symbol]) for symbol in ranked}


def signal_date(series, latest_day, state):
    """Initial entry uses the last completed session, then month-first closes."""
    if not state.get("rotation_month"):
        return latest_day
    month = latest_day[:7]
    if state["rotation_month"] >= month:
        return None
    candidates = [r["date"] for r in series[ETF_UNIVERSE[0]] if r["date"][:7] == month]
    return min(candidates) if candidates else None


def decide_rotation(spec, state, quotes, series, at, previous_session):
    """One marketable limit order at a time; sell before buying with settled cash."""
    latest_day = previous_session
    if not series or any(not series.get(s) or series[s][-1]["date"] != latest_day for s in ETF_UNIVERSE):
        return None, "직전 정규장까지의 ETF 7개 일봉 수집 대기"
    stamp = signal_date(series, latest_day, state)
    if stamp:
        weights, scores = monthly_targets(series, stamp)
        state.update(rotation_month=stamp[:7], target_date=stamp, targets=weights, ranking=scores)
    weights = {symbol: D(value) for symbol, value in state.get("targets", {}).items()}
    required = set(weights) | {symbol for symbol in ETF_UNIVERSE if held(state, symbol) > 0}
    for symbol in required:
        quote = quotes.get(symbol)
        if not quote or not quote.valid(spec, at):
            return None, f"{symbol}의 최신 양방향 호가·호가 차이 확인 대기"
    # Daily rebalance decisions use the prior close, not oscillating intraday weights.
    prices = {symbol: D(str(series[symbol][-1]["close"])) for symbol in ETF_UNIVERSE}
    return allocation_decision(spec, state, quotes, prices, weights, ETF_UNIVERSE, at)


def allocation_decision(spec, state, quotes, prices, weights, universe, at):
    """Shared long-only allocation and serial sell-before-buy sizing."""
    nav = D(state["cash"]) + sum((held(state, s) * prices[s] for s in universe), D(0))
    if nav <= 0:
        return None, "ETF 공동 운용 자금 확인 필요"
    plan_day = at.astimezone(NEW_YORK).date().isoformat()
    if state.get("plan_day") != plan_day:
        goals = {}
        for symbol in universe:
            quantity = held(state, symbol)
            target = weights.get(symbol, D(0))
            difference = target - quantity * prices[symbol] / nav
            if target == 0:
                goal = D(0)
            elif abs(difference) >= REBALANCE_BAND:
                goal = round_quantity(nav * target / prices[symbol], spec.venue)
            else:
                goal = quantity
            goals[symbol] = str(goal)
        state.update(plan_day=plan_day, goals=goals)
    goals = {s: D(v) for s, v in state["goals"].items()}
    for side in ("SELL", "BUY"):
        for symbol in universe:
            delta = goals[symbol] - held(state, symbol)
            if (side == "SELL" and delta >= 0) or (side == "BUY" and delta <= 0):
                continue
            quote = quotes.get(symbol)
            if not quote or not quote.valid(spec, at):
                return None, f"{symbol} 최신 호가 확인 대기"
            price = tick(quote.bid if side == "SELL" else quote.ask, spec.venue)
            quantity = abs(delta)
            if side == "BUY":
                available = round_quantity(
                    D(state["cash"]) / (price * (1 + spec.commission_rate)), spec.venue
                )
                quantity = min(quantity, available)
            if quantity > 0 and order_size_valid(quantity, price, spec.venue):
                return Decision(
                    side, quantity, price, "ETF 월간 선정·일별 비중 조절", symbol=symbol
                ), "ETF 비중 조절 주문 준비"
    return None, "ETF 목표 비중 유지 — 다음 정규장 또는 월별 선정 대기"


def summary():
    return {
        "symbols": list(ETF_UNIVERSE),
        "lookback_sessions": LOOKBACK,
        "top": TOP,
        "weight_each": "0.5",
        "rebalance_band": str(REBALANCE_BAND),
        "ranking_schedule": "매월 첫 거래일 종가 확정 후 다음 정규장",
        "initial_entry": "최초 시작은 직전 완성 정규장 종가 기준",
        "negative_momentum": "배정 몫은 USD 현금 유지",
        "sessions": "미국 정규장",
        "dividends": "순위에는 반영, 실제 입금은 전략 현금에 자동 편입하지 않음",
    }
