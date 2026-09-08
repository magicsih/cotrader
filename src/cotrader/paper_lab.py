"""Frozen, independently funded forward experiments. No broker order capability."""

import asyncio
import contextlib
import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select

from cotrader.broker import BrokerError
from cotrader.domain import ETF_UNIVERSE, D, initial_state, paper_execution
from cotrader.etf_data import calendar, last_completed_session
from cotrader.models import PaperEvent, PaperPortfolio, Strategy
from cotrader.rotation import NEW_YORK, allocation_decision, apply_execution, held
from cotrader.services import put_runtime

CRYPTO_UNIVERSE = ("USDT-BTC", "USDT-SOL")
VARIANTS = ("base", "buffer", "slow", "hold")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def policy(venue, variant):
    etf = venue == "toss"
    return {
        "version": 1,
        "variant": variant,
        "symbols": list(ETF_UNIVERSE if etf else CRYPTO_UNIVERSE),
        "lookback": (12 if variant == "slow" else 10) if etf else (250 if variant == "slow" else 200),
        "schedule": "completed_month" if etf else "completed_utc_day",
        "buffer": ("0.005" if etf else "0.01") if variant == "buffer" else "0",
        "fee": "0.001" if etf else "0.0005",
        "execution_cost": "0.0005",
        "max_spread_bps": "30",
        "quote_max_age": 10,
        "depth_fraction": "0.1",
        "order_ttl_seconds": 60,
        "rebalance_band": "0.05",
        "leverage": "1",
        "shorting": False,
        "dividends": "gross_ex_date_receivable_not_reinvested",
        "evaluation_days": 90,
        "minimum_sell_orders": 10,
    }


@dataclass(frozen=True)
class ExecutionSpec:
    venue: str
    symbols: tuple[str, ...]
    commission_rate: D
    max_spread_bps: D = D("30")
    quote_max_age: int = 10
    execution_policy: str = "trigger_limit"


def spec_for(row):
    return ExecutionSpec(
        row.venue, tuple(row.rules["symbols"]), D(row.rules["fee"]) + D(row.rules["execution_cost"])
    )


def signal(rules, series, at, previous):
    """Completed data only; missing bars never become fabricated flat returns."""
    symbols = rules["symbols"]
    if set(series) != set(symbols):
        raise ValueError("모든 종목의 완성 일봉 수집 대기")
    etf = rules["schedule"] == "completed_month"
    latest = last_completed_session(at) if etf else (at.date() - timedelta(days=1)).isoformat()
    dates = [r["date"] for r in series[symbols[0]]]
    if not dates or dates != sorted(set(dates)) or dates[-1] != latest:
        raise ValueError("직전 완성 일봉 확인 대기")
    if etf:
        expected = [s.date().isoformat() for s in calendar(at.year).sessions_in_range(dates[0], latest)]
    else:
        first = date.fromisoformat(dates[0])
        expected = [
            (first + timedelta(days=i)).isoformat()
            for i in range((date.fromisoformat(latest) - first).days + 1)
        ]
    if dates != expected:
        raise ValueError("일봉 누락: 해당 기간을 임의로 채우지 않고 대기")
    weights, details, stamp = {}, {}, None
    for symbol in symbols:
        rows = series[symbol]
        if [r["date"] for r in rows] != dates:
            raise ValueError("종목별 완성 일봉 날짜 불일치")
        if etf:
            months = {}
            for r in rows:
                if r["date"][:7] < at.astimezone(NEW_YORK).strftime("%Y-%m"):
                    months[r["date"][:7]] = r
            samples = list(months.values())
        else:
            samples = rows
        samples = samples[-rules["lookback"] :]
        if len(samples) < rules["lookback"]:
            raise ValueError("추세 판단에 필요한 완성 기간 부족")
        values = [D(r["total_return"] if etf else r["close"]) for r in samples]
        if any(not p.is_finite() or p <= 0 for p in values):
            raise ValueError("종가 또는 배당 포함 지수 오류")
        mean, value = sum(values) / len(values), values[-1]
        buffer = D(rules["buffer"])
        was_in = D(previous.get(symbol, "0")) > 0
        enter, exit_ = value > mean * (1 + buffer), value < mean * (1 - buffer)
        active = rules["variant"] == "hold" or (not exit_ if was_in else enter)
        weights[symbol] = str(D(1) / len(symbols) if active else D(0))
        stamp = samples[-1]["date"]
        details[symbol] = {
            "value": str(value),
            "average": str(mean),
            "enter_pass": enter,
            "exit_pass": exit_,
            "target": weights[symbol],
            "signal_date": stamp,
        }
    return stamp, weights, details


async def crypto_history(broker, at):
    series = {}
    end = at.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    for symbol in CRYPTO_UNIVERSE:
        before, rows = end, []
        for _ in range(2):
            page = await broker.candles(symbol, "1d", before)
            rows.extend(page["candles"])
            if not page["nextBefore"] or page["nextBefore"] >= before:
                raise ValueError("암호화폐 일봉 페이지 진행 오류")
            before = page["nextBefore"]
        series[symbol] = [
            {"date": datetime.fromisoformat(r["timestamp"]).date().isoformat(), "close": r["closePrice"]}
            for r in sorted(rows, key=lambda r: r["timestamp"])
        ]
    result = {"series": series, "checked_at": at.isoformat(), "source": "Upbit completed daily candles"}
    result["fingerprint"] = digest(series)
    return result


def next_check(venue, at):
    if venue != "toss":
        return (at + timedelta(days=1)).replace(hour=0, minute=0, second=10, microsecond=0).isoformat()
    market = calendar(at.year)
    day = market.date_to_session(at.astimezone(NEW_YORK).date().isoformat(), direction="next")
    if market.session_close(day).to_pydatetime() <= at:
        day = market.next_session(day)
    return max(at + timedelta(seconds=10), market.session_open(day).to_pydatetime()).isoformat()


def add_event(session, row, kind, key, data, at):
    session.add(
        PaperEvent(
            portfolio_id=row.id,
            kind=kind,
            fingerprint=f"{row.id}:{key}",
            data=data,
            created_at=at.replace(tzinfo=None),
        )
    )


async def distributions(session, row, state, series, at):
    """Ex-date rights use fills before that session, including sales on the ex-date."""
    if row.venue != "toss":
        return
    paid = dict(state.get("distributions", {}))
    for symbol, rows in series.items():
        for bar in rows:
            dividend = D(bar.get("dividend", "0"))
            key = f"{symbol}:{bar['date']}"
            if dividend <= 0 or key in paid or bar["date"] < row.created_at.date().isoformat():
                continue
            opened = calendar(at.year).session_open(bar["date"]).to_pydatetime().replace(tzinfo=None)
            if opened > at.replace(tzinfo=None):
                continue
            fills = (
                await session.scalars(
                    select(PaperEvent).where(
                        PaperEvent.portfolio_id == row.id,
                        PaperEvent.kind == "fill",
                        PaperEvent.created_at < opened,
                    )
                )
            ).all()
            quantity = sum(
                (
                    D(e.data["quantity"]) * (1 if e.data["side"] == "BUY" else -1)
                    for e in fills
                    if e.data["symbol"] == symbol
                ),
                D(0),
            )
            value = quantity * dividend
            paid[key] = str(value)
            add_event(
                session,
                row,
                "dividend",
                f"div:{key}",
                {
                    "symbol": symbol,
                    "ex_date": bar["date"],
                    "eligible_quantity": str(quantity),
                    "amount": str(value),
                },
                at,
            )
    state["distributions"] = paid
    state["dividend_receivable"] = str(sum((D(v) for v in paid.values()), D(0)))


async def advance(session, row, history, quotes, at, market_open):
    state = deepcopy(row.state)
    spec = spec_for(row)
    series = (history or {}).get("series", {})
    reason, action, fresh_data = "", "WAIT", False
    try:
        stamp, weights, details = signal(row.rules, series, at, state.get("targets", {}))
        await distributions(session, row, state, history.get("distributions", series), at)
        if state.get("signal_date") != stamp:
            state.update(signal_date=stamp, targets=weights, conditions=details)
            if row.rules["variant"] != "hold" or not state.get("goals"):
                state.pop("plan_day", None)
        state["history_fingerprint"] = history["fingerprint"]
        fresh_data = True
    except (ValueError, KeyError, ArithmeticError) as exc:
        reason = str(exc)
    if not market_open:
        reason = reason or "미국 정규장 시작 대기"
    ready = fresh_data and market_open
    pending = state.get("pending")
    if pending:
        submitted = datetime.fromisoformat(pending["created_at"])
        if not ready or at - submitted >= timedelta(seconds=60):
            add_event(
                session,
                row,
                "cancel",
                f"cancel:{pending['id']}",
                {**pending, "reason": reason or "60초 미체결 만료"},
                at,
            )
            state.pop("pending")
            state["retry_after"] = (at + timedelta(seconds=60)).isoformat()
        else:
            quote = quotes.get(pending["symbol"])
            executed = paper_execution(
                spec,
                quote,
                pending["side"],
                D(pending["price"]),
                D(pending["remaining"]),
                datetime.fromisoformat(pending["after"]),
                at,
            )
            if executed:
                qty, price = executed
                amount = qty * price
                fee = amount * D(row.rules["fee"])
                extra = amount * D(row.rules["execution_cost"])
                apply_execution(
                    state, pending["symbol"], pending["side"], qty, amount, fee + extra, spec.symbols
                )
                if D(state["cash"]) < 0 or any(held(state, s) < 0 for s in spec.symbols):
                    raise ValueError("모의 계좌 잔고 불일치")
                add_event(
                    session,
                    row,
                    "fill",
                    f"fill:{pending['id']}:{quote.at.isoformat()}",
                    {
                        "order_id": pending["id"],
                        "symbol": pending["symbol"],
                        "side": pending["side"],
                        "quantity": str(qty),
                        "price": str(price),
                        "amount": str(amount),
                        "fee": str(fee),
                        "execution_cost": str(extra),
                        "quote_at": quote.at.isoformat(),
                        "rules_hash": row.rules_hash,
                    },
                    at,
                )
                pending.update(remaining=str(D(pending["remaining"]) - qty), after=quote.at.isoformat())
                state["fill_count"] = state.get("fill_count", 0) + 1
                if D(pending["remaining"]) == 0:
                    state.pop("pending")
            reason = "지정가 이후 새로운 호가에서 모의 체결 확인 중"
    if ready and not state.get("pending") and at.isoformat() >= state.get("retry_after", ""):
        prices = {s: D(series[s][-1]["close"]) for s in spec.symbols}
        if row.rules["variant"] == "hold" and state.get("goals"):
            state["plan_day"] = at.astimezone(NEW_YORK).date().isoformat()
        decision, _ = allocation_decision(
            spec, state, quotes, prices, {s: D(v) for s, v in state["targets"].items()}, spec.symbols, at
        )
        if decision:
            quote = quotes[decision.symbol]
            pending = {
                "id": str(uuid4()),
                "symbol": decision.symbol,
                "side": decision.side,
                "quantity": str(decision.quantity),
                "remaining": str(decision.quantity),
                "price": str(decision.price),
                "created_at": at.isoformat(),
                "after": max(at, quote.at).isoformat(),
            }
            state["pending"] = pending
            add_event(
                session,
                row,
                "order",
                f"order:{pending['id']}",
                {**pending, "signal_date": state["signal_date"], "rules_hash": row.rules_hash},
                at,
            )
            action, reason = (
                decision.side,
                f"{decision.symbol} 목표 수량 {state['goals'][decision.symbol]}에 맞춰 모의 {decision.side}",
            )
        elif any(not quotes.get(s) or not quotes[s].valid(spec, at) for s in spec.symbols):
            reason = "최신 양방향 호가 또는 허용 호가 차이 확인 대기"
        else:
            action, reason = "HOLD", "목표 비중 유지: 추세 조건을 충족하지 않는 몫은 현금 보유"
    elif state.get("pending"):
        action = state["pending"]["side"]
    state.update(
        action=action,
        reason=reason or "다음 평가 대기",
        checked_at=at.isoformat(),
        next_check_at=(at + timedelta(seconds=5)).isoformat() if market_open else next_check(row.venue, at),
    )
    key = digest(
        {
            "action": action,
            "reason": state["reason"],
            "signal_date": state.get("signal_date"),
            "conditions": state.get("conditions"),
        }
    )
    if state.get("decision_key") != key:
        add_event(
            session,
            row,
            "decision",
            f"decision:{uuid4()}",
            {
                k: state.get(k)
                for k in (
                    "action",
                    "reason",
                    "signal_date",
                    "conditions",
                    "checked_at",
                    "next_check_at",
                    "history_fingerprint",
                )
            },
            at,
        )
        state["decision_key"] = key
    if fresh_data:
        # Fresh bid valuations while open; completed official closes while ETF is closed.
        marks = {}
        for symbol in spec.symbols:
            quote = quotes.get(symbol)
            if not market_open and row.venue == "toss":
                marks[symbol] = D(series[symbol][-1]["close"])
            elif quote and quote.valid(spec, at):
                marks[symbol] = quote.bid
            elif held(state, symbol) > 0:
                break
            else:
                marks[symbol] = D(0)
        if len(marks) == len(spec.symbols):
            nav = (
                D(state["cash"])
                + D(state.get("dividend_receivable", "0"))
                + sum((held(state, s) * marks[s] for s in spec.symbols), D(0))
            )
            high = max(nav, D(state.get("high_water", str(row.budget))))
            dd = (high - nav) / high
            state.update(
                nav=str(nav),
                nav_at=at.isoformat(),
                high_water=str(high),
                max_drawdown=str(max(dd, D(state.get("max_drawdown", "0")))),
                return_pct=str((nav / row.budget - 1) * 100),
                valuation="bid" if market_open else "previous_close",
                observation_day=at.astimezone(NEW_YORK).date().isoformat()
                if row.venue == "toss" and market_open
                else series[spec.symbols[0]][-1]["date"],
            )
            slot = at.strftime("%Y-%m-%dT%H:") + str(at.minute // 15)
            if state.get("nav_slot") != slot:
                add_event(
                    session,
                    row,
                    "nav",
                    f"nav:{slot}",
                    {
                        k: state[k]
                        for k in (
                            "nav",
                            "return_pct",
                            "max_drawdown",
                            "valuation",
                            "nav_at",
                            "observation_day",
                        )
                    },
                    at,
                )
                state["nav_slot"] = slot
    row.state = state


async def bootstrap_lab(session, settings, at):
    active = await session.scalar(
        select(Strategy.id).where(Strategy.mode == "live", Strategy.status != "ARCHIVED").limit(1)
    )
    if active:
        raise ValueError("실거래 전략을 모두 종료한 후 모의 실험을 시작하세요")
    for venue, budget in (("toss", settings.paper_lab_usd), ("upbit_usdt", settings.paper_lab_usdt)):
        for variant in VARIANTS:
            ident = f"{venue}-{variant}-v1"
            rules = policy(venue, variant)
            row = await session.get(PaperPortfolio, ident)
            if row:
                if row.rules_hash != digest(rules) or row.rules != rules or row.budget != budget:
                    raise ValueError("진행 중인 실험의 규칙·예산은 변경할 수 없습니다. 새 버전이 필요합니다")
                continue
            name = {
                "base": "기준 추세",
                "buffer": "후보 · 진입과 이탈 여유 폭",
                "slow": "후보 · 더 긴 추세 기간",
                "hold": "비교 · 최초 매수 후 보유",
            }[variant]
            session.add(
                PaperPortfolio(
                    id=ident,
                    venue=venue,
                    name=name,
                    rules=rules,
                    rules_hash=digest(rules),
                    budget=budget,
                    state={
                        **initial_state(budget),
                        "positions": {},
                        "action": "WAIT",
                        "reason": "완성 일봉과 실제 호가 수집 대기",
                        "nav": str(budget),
                    },
                    created_at=at.replace(tzinfo=None),
                )
            )


async def report(session, at):
    rows = (
        await session.scalars(select(PaperPortfolio).order_by(PaperPortfolio.venue, PaperPortfolio.id))
    ).all()
    result = []
    for row in rows:
        recent = (
            await session.scalars(
                select(PaperEvent)
                .where(PaperEvent.portfolio_id == row.id, PaperEvent.kind.in_(["fill", "cancel", "dividend"]))
                .order_by(PaperEvent.created_at.desc())
                .limit(12)
            )
        ).all()
        age = (at.replace(tzinfo=None) - row.created_at).days
        result.append(
            {
                "id": row.id,
                "venue": row.venue,
                "name": row.name,
                "rules": row.rules,
                "rules_hash": row.rules_hash,
                "budget": str(row.budget),
                "state": row.state,
                "started_at": row.created_at.isoformat() + "Z",
                "days": age,
                "recent": [{"kind": e.kind, "at": e.created_at.isoformat() + "Z", **e.data} for e in recent],
            }
        )
    evaluations = []
    for venue in ("toss", "upbit_usdt"):
        group = [r for r in rows if r.venue == venue]
        if len(group) != 4:
            continue
        base = next(r for r in group if r.rules["variant"] == "base")
        days = min((at.replace(tzinfo=None) - r.created_at).days for r in group)
        sells, observed = {}, []
        for r in group:
            sells[r.id] = await session.scalar(
                select(func.count(func.distinct(PaperEvent.data["order_id"].as_string()))).where(
                    PaperEvent.portfolio_id == r.id,
                    PaperEvent.kind == "fill",
                    PaperEvent.data["side"].as_string() == "SELL",
                )
            )
            navs = (
                await session.scalars(
                    select(PaperEvent.data["observation_day"].as_string())
                    .where(PaperEvent.portfolio_id == r.id, PaperEvent.kind == "nav")
                    .distinct()
                )
            ).all()
            observed.append(set(navs))
        common_days = len(set.intersection(*observed))
        fresh = all(
            r.state.get("nav_at") and at - datetime.fromisoformat(r.state["nav_at"]) < timedelta(minutes=2)
            for r in group
        )
        due = base.created_at + timedelta(days=((max(0, days) // 90) + 1) * 90)
        # These are predeclared screening criteria, never evidence of guaranteed profit.
        candidates = []
        if (
            days >= 90
            and common_days >= (50 if venue == "toss" else 80)
            and fresh
            and min(sells[r.id] for r in group if r.rules["variant"] != "hold") >= 10
        ):
            for r in group:
                if (
                    r.rules["variant"] in {"buffer", "slow"}
                    and D(r.state.get("return_pct", "0"))
                    > max(D(0), D(base.state.get("return_pct", "0"))) + 1
                    and D(r.state.get("max_drawdown", "1")) <= D(base.state.get("max_drawdown", "0"))
                ):
                    candidates.append(r.id)
        evaluations.append(
            {
                "venue": venue,
                "status": "REVIEW" if candidates else "KEEP_COLLECTING",
                "reason": "기준보다 비용 후 수익률이 1%p 이상 높고 손실 폭이 작거나 같은 후보: 추가 검증 검토"
                if candidates
                else "90일 이상 및 전략별 매도 주문 10건을 포함한 관찰과 개선 근거가 필요합니다",
                "candidate_ids": candidates,
                "sell_orders": sells,
                "common_observation_days": common_days,
                "fresh_valuations": fresh,
                "next_review_at": due.isoformat() + "Z",
            }
        )
    return {
        "portfolios": result,
        "evaluations": evaluations,
        "actual_order_created": False,
        "at": at.isoformat(),
    }


class PaperLab:
    def __init__(self, settings, sessions, upbit):
        self.settings, self.sessions, self.upbit = settings, sessions, upbit
        self.history, self.task = None, None
        self.last_refresh = datetime.min.replace(tzinfo=UTC)
        self.last_step = self.last_refresh
        self.error = ""
        self.review_day = ""

    async def close(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, BrokerError, ValueError, KeyError):
                await self.task

    async def step(self, at, quotes, etf_history, etf_open):
        if (at - self.last_step).total_seconds() < 5:
            return
        self.last_step = at
        if self.task and self.task.done():
            try:
                self.history = self.task.result()
                self.error = ""
                async with self.sessions.begin() as session:
                    await put_runtime(session, "paper_crypto_history", self.history)
            except (BrokerError, ValueError, KeyError, TypeError) as exc:
                self.error = f"암호화폐 일봉 조회 확인 필요: {type(exc).__name__}"
            self.task = None
            self.last_refresh = at
        if self.task is None and (at - self.last_refresh).total_seconds() >= (60 if self.error else 1800):
            self.task = asyncio.create_task(crypto_history(self.upbit, at))
        async with self.sessions.begin() as session:
            await bootstrap_lab(session, self.settings, at)
            await session.flush()
            rows = (await session.scalars(select(PaperPortfolio).with_for_update())).all()
            for row in rows:
                await advance(
                    session,
                    row,
                    etf_history if row.venue == "toss" else self.history,
                    quotes,
                    at,
                    etf_open if row.venue == "toss" else True,
                )
            if self.review_day != at.date().isoformat():
                await session.flush()
                evaluation = await report(session, at)
                for item in evaluation["evaluations"]:
                    base = next(r for r in rows if r.venue == item["venue"] and r.rules["variant"] == "base")
                    key = f"review:{at.date().isoformat()}"
                    if not await session.scalar(
                        select(PaperEvent.id).where(PaperEvent.fingerprint == f"{base.id}:{key}")
                    ):
                        add_event(session, base, "review", key, item, at)
            await put_runtime(
                session,
                "paper_lab",
                {"enabled": True, "checked_at": at.isoformat(), "crypto_data_error": self.error},
            )
        self.review_day = at.date().isoformat()
