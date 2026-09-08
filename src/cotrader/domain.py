from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from cotrader.markets import (
    UpbitCaution,
    Venue,
    currency,
    is_upbit,
    order_size_valid,
    price_tick,
    round_quantity,
    validate_symbol,
)

D = Decimal
ZERO = D(0)
ACTIVE_ORDERS = {"PREPARED", "SENDING", "UNKNOWN", "PENDING", "PARTIAL_FILLED", "PENDING_CANCEL"}
ETF_UNIVERSE = ("SPY", "QQQ", "IWM", "IEF", "TLT", "GLD", "SHY")


class StrategySpec(BaseModel):
    venue: Venue = "toss"
    symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.\-]{0,23}$")
    kind: Literal["grid", "trend", "rebound", "rotation"] = "grid"
    rotation_policy: Literal["monthly_252_top2_v1"] | None = None
    budget: Decimal = Field(gt=0, le=10000000)
    inventory_quantity: Decimal = Field(default=D(0), ge=0)
    inventory_reference_price: Decimal | None = Field(default=None, gt=0)
    inventory_average_price: Decimal | None = Field(default=None, ge=0)
    inventory_average_currency: str | None = Field(default=None, pattern=r"^(KRW|USDT)$")
    execution_policy: Literal["trigger_limit", "maker_only"] = "trigger_limit"
    allowed_market_cautions: tuple[UpbitCaution, ...] = ()
    lower: Decimal | None = Field(default=None, gt=0)
    upper: Decimal | None = Field(default=None, gt=0)
    grids: int = Field(default=5, ge=2, le=30)
    spacing: Literal["arithmetic", "geometric"] = "geometric"
    signal_gate: bool = False
    timeframe: int = Field(default=15, ge=1, le=240)
    fast: int = Field(default=20, ge=2, le=100)
    slow: int = Field(default=60, ge=3, le=240)
    rsi_period: int = Field(default=14, ge=2, le=100)
    rsi_entry: int = Field(default=30, ge=5, le=45)
    rsi_exit: int = Field(default=55, ge=50, le=90)
    max_spread_bps: Decimal = Field(default=D("30"), gt=0, le=100)
    quote_max_age: int = Field(default=10, ge=1, le=60)
    commission_rate: Decimal = Field(default=D("0.001"), ge=0, le=D("0.02"))
    slippage_bps: Decimal = Field(default=D("10"), ge=0, le=100)

    @model_validator(mode="after")
    def validate_grid(self):
        validate_symbol(self.venue, self.symbol)
        if self.kind == "rotation":
            self.rotation_policy = "monthly_252_top2_v1"
        elif self.rotation_policy is not None:
            raise ValueError("월간 ETF 정책은 ETF 교체 전략에서만 사용할 수 있습니다")
        if self.kind == "rotation" and (
            self.venue != "toss"
            or self.symbol != "ETF-ROTATION"
            or self.inventory_quantity
            or self.execution_policy != "trigger_limit"
            or self.signal_gate
            or self.lower is not None
            or self.upper is not None
        ):
            raise ValueError("ETF 교체 전략은 토스 ETF-ROTATION, 현금 시작, 지정가 주문으로 설정하세요")
        if self.allowed_market_cautions and not is_upbit(self.venue):
            raise ValueError("주의 항목 허용은 업비트 전략에서만 설정할 수 있습니다")
        self.allowed_market_cautions = tuple(sorted(set(self.allowed_market_cautions)))
        if self.execution_policy == "maker_only" and not (
            is_upbit(self.venue) and self.kind == "grid" and self.inventory_quantity
        ):
            raise ValueError("메이커 전용 주문은 보유 코인 반복 그리드에서 지원합니다")
        if self.inventory_average_price is not None and self.inventory_average_currency is None:
            self.inventory_average_currency = currency(self.venue)
        if self.inventory_average_currency and self.inventory_average_price is None:
            raise ValueError("평균 매수가 없이 평균가 통화를 지정할 수 없습니다")
        if self.inventory_quantity:
            if not is_upbit(self.venue) or self.kind != "grid":
                raise ValueError("보유 코인 편입은 업비트 반복 그리드에서만 지원합니다")
            if self.signal_gate:
                raise ValueError(
                    "보유 시작 그리드는 단계별 가격 조건으로 재매수합니다. 신호 필터를 함께 지정할 수 없습니다"
                )
            if (
                self.inventory_reference_price is None
                or self.inventory_quantity != round_quantity(self.inventory_quantity, self.venue)
                or self.budget != self.inventory_quantity * self.inventory_reference_price
            ):
                raise ValueError("편입 수량·평가 기준가·배정 평가금액이 일치해야 합니다")
        elif self.inventory_reference_price is not None or self.inventory_average_price is not None:
            raise ValueError("편입 수량 없이 보유 코인 기준가를 지정할 수 없습니다")
        if self.venue == "toss" and self.budget > 5000:
            raise ValueError("미국 주식의 전략 예산은 최대 5,000 USD입니다")
        if self.venue == "upbit_usdt" and self.budget > 10000:
            raise ValueError("USDT 전략 예산은 최대 10,000 USDT입니다")
        if self.fast >= self.slow:
            raise ValueError("단기 평균 기간은 장기 평균 기간보다 작아야 합니다")
        if self.kind == "grid":
            if self.lower is None or self.upper is None or self.lower >= self.upper:
                raise ValueError("그리드 하단과 상단을 올바르게 입력하세요")
            prices = levels(self)
            if len(set(prices)) != len(prices):
                raise ValueError("호가 단위 반올림 후 가격선이 중복됩니다")
            if any(
                not order_size_valid(grid_quantity(self, i), p, self.venue) for i, p in enumerate(prices[:-1])
            ):
                raise ValueError(
                    "가격선마다 최소 1주, 5,000 KRW 또는 0.5 USDT가 필요합니다. 예산을 늘리거나 단계를 줄이세요"
                )
            minimum_gap = min((b - a) / a for a, b in zip(prices, prices[1:], strict=False))
            if minimum_gap <= self.commission_rate * 2 + self.slippage_bps / D(10000) * 2:
                raise ValueError("그리드 간격이 왕복 수수료와 가정한 체결 비용보다 좁습니다")
        return self

    @property
    def symbols(self) -> tuple[str, ...]:
        return ETF_UNIVERSE if self.kind == "rotation" else (self.symbol,)


def tick(price: Decimal, venue: Venue = "toss") -> Decimal:
    return price_tick(price, venue)


def levels(spec: StrategySpec) -> list[Decimal]:
    if spec.spacing == "arithmetic":
        return [
            tick(spec.lower + (spec.upper - spec.lower) * D(i) / spec.grids, spec.venue)
            for i in range(spec.grids + 1)
        ]
    ratio = (spec.upper / spec.lower) ** (D(1) / spec.grids)
    return [tick(spec.lower * ratio**i, spec.venue) for i in range(spec.grids)] + [
        tick(spec.upper, spec.venue)
    ]


def slot_quantity(spec: StrategySpec, price: Decimal) -> Decimal:
    return round_quantity(spec.budget * D("0.98") / spec.grids / price, spec.venue)


def grid_quantity(spec: StrategySpec, slot: int) -> Decimal:
    if not spec.inventory_quantity:
        return slot_quantity(spec, levels(spec)[slot])
    size = round_quantity(spec.inventory_quantity / spec.grids, spec.venue)
    return size if slot < spec.grids - 1 else spec.inventory_quantity - size * (spec.grids - 1)


def funded_state(spec: StrategySpec) -> dict:
    if not spec.inventory_quantity:
        return initial_state(spec.budget)
    state = initial_state(D(0))
    state.update(
        quantity=str(spec.inventory_quantity),
        cost_basis=str(spec.budget),
        lots={
            str(i): {
                "quantity": str(grid_quantity(spec, i)),
                "cost": str(grid_quantity(spec, i) * spec.inventory_reference_price),
                "cash": "0",
            }
            for i in range(spec.grids)
        },
    )
    return state


def initial_state(budget: Decimal) -> dict:
    return {
        "cash": str(budget),
        "quantity": "0",
        "cost_basis": "0",
        "realized": "0",
        "costs": "0",
        "lots": {},
        "last_mid": None,
        "last_bar": None,
    }


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    at: datetime
    bid_size: Decimal = D(0)
    ask_size: Decimal = D(0)

    @property
    def mid(self):
        return (self.bid + self.ask) / 2

    def fresh(self, spec: StrategySpec, at: datetime) -> bool:
        age = (at - self.at).total_seconds()
        return bool(
            self.symbol in spec.symbols
            and self.bid > 0
            and self.ask >= self.bid
            and 0 <= age <= spec.quote_max_age
        )

    def valid(self, spec: StrategySpec, at: datetime) -> bool:
        return self.fresh(spec, at) and (self.ask - self.bid) / self.mid * 10000 <= spec.max_spread_bps


def paper_execution(spec, quote, side, limit, remaining, after, at):
    """Fill only a later, valid quote; share one depth convention across paper paths."""
    if not quote or not quote.valid(spec, at) or quote.at <= after:
        return None
    price = quote.ask if side == "BUY" else quote.bid
    crosses = price <= limit if side == "BUY" else price >= limit
    depth = quote.ask_size if side == "BUY" else quote.bid_size
    quantity = min(remaining, round_quantity(depth * D("0.1"), spec.venue))
    if not crosses or quantity <= 0:
        return None
    return quantity, limit if spec.execution_policy == "maker_only" else price


@dataclass(frozen=True)
class Bar:
    at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @classmethod
    def parse(cls, value: dict):
        timestamp = datetime.fromisoformat(value["timestamp"])
        if timestamp.tzinfo is None:
            raise ValueError("시세 시각에는 UTC 또는 시간대 오프셋이 필요합니다")
        return cls(
            timestamp.astimezone(UTC),
            *[D(value[key]) for key in ("openPrice", "highPrice", "lowPrice", "closePrice", "volume")],
        )

    def json(self):
        return dict(
            zip(
                ("timestamp", "openPrice", "highPrice", "lowPrice", "closePrice", "volume"),
                (
                    self.at.isoformat(),
                    str(self.open),
                    str(self.high),
                    str(self.low),
                    str(self.close),
                    str(self.volume),
                ),
                strict=True,
            )
        )


@dataclass(frozen=True)
class Decision:
    side: str
    quantity: Decimal
    price: Decimal
    reason: str
    slot: int | None = None
    symbol: str | None = None


def ema(values: list[Decimal], period: int) -> list[Decimal]:
    if not values:
        return []
    alpha = D(2) / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def rsi(values: list[Decimal], period: int) -> list[Decimal]:
    if len(values) <= period:
        return []
    diffs = [b - a for a, b in zip(values, values[1:], strict=False)]
    gain = sum(max(d, ZERO) for d in diffs[:period]) / period
    loss = sum(max(-d, ZERO) for d in diffs[:period]) / period

    def score():
        return D(50) if gain == loss == 0 else D(100) if loss == 0 else 100 - 100 / (1 + gain / loss)

    result = [score()]
    for diff in diffs[period:]:
        gain = (gain * (period - 1) + max(diff, ZERO)) / period
        loss = (loss * (period - 1) + max(-diff, ZERO)) / period
        result.append(score())
    return result


def aggregate(bars: list[Bar], minutes: int, at: datetime) -> list[Bar]:
    """Only complete buckets; never fabricate missing minutes."""
    groups: dict[int, list[Bar]] = {}
    width = minutes * 60
    for bar in bars:
        bucket = int(bar.at.timestamp()) // width * width
        if bucket + width <= at.timestamp():
            groups.setdefault(bucket, []).append(bar)
    result = []
    for bucket, group in sorted(groups.items()):
        group.sort(key=lambda b: b.at)
        if len({int(b.at.timestamp()) for b in group}) != minutes:
            continue
        result.append(
            Bar(
                datetime.fromtimestamp(bucket, UTC),
                group[0].open,
                max(b.high for b in group),
                min(b.low for b in group),
                group[-1].close,
                sum(b.volume for b in group),
            )
        )
    return result


def decide(
    spec: StrategySpec, state: dict, quote: Quote, bars: list[Bar], *, blocked_slots=frozenset()
) -> tuple[Decision | None, str]:
    if spec.kind == "rotation":
        raise ValueError("ETF 교체 전략은 여러 종목의 일봉과 공동 예산 판단이 필요합니다")
    if spec.inventory_quantity:
        if spec.execution_policy == "maker_only":
            return maker_grid_decision(spec, state, quote, blocked_slots)
        return inventory_decision(spec, state, quote)
    if spec.kind == "grid":
        if quote.mid < spec.lower:
            return None, "GRID_LOWER_BREACH"
        previous = D(state["last_mid"]) if state.get("last_mid") is not None else None
        state["last_mid"] = str(quote.mid)
        if previous is None:
            return None, "그리드 감시 시작 — 최초 시세에서 주문하지 않습니다"
        prices = levels(spec)
        # Owned inventory takes priority. A gap never generates a cascade of catch-up buys.
        sellable = [
            (int(i), lot)
            for i, lot in state["lots"].items()
            if D(lot["quantity"]) > 0 and quote.bid >= prices[int(i) + 1]
        ]
        if sellable:
            slot, lot = min(sellable, key=lambda pair: pair[0])
            if not order_size_valid(D(lot["quantity"]), prices[slot + 1], spec.venue):
                return None, "보유 잔량의 최소 주문 금액 충족 대기"
            return Decision(
                "SELL", D(lot["quantity"]), prices[slot + 1], "그리드 익절 가격 도달", slot
            ), "매도 신호"
        if spec.signal_gate:
            closes = [b.close for b in bars]
            if len(closes) < spec.slow + 1:
                return None, "신호 계산용 완성 봉 부족"
            if (quote.at - bars[-1].at).total_seconds() > spec.timeframe * 120:
                return None, "최신 완성 봉 수집 대기"
            fast, slow = ema(closes, spec.fast), ema(closes, spec.slow)
            if fast[-1] < slow[-1] and fast[-1] < fast[-2]:
                return None, "하락 추세 — 신규 그리드 매수 보류"
        candidates = [
            i
            for i, price in enumerate(prices[:-1])
            if quote.ask <= price < previous and D(state["lots"].get(str(i), {}).get("quantity", "0")) == 0
        ]
        if candidates:
            slot = min(candidates, key=lambda i: abs(prices[i] - quote.ask))
            return Decision(
                "BUY", slot_quantity(spec, prices[slot]), prices[slot], "그리드 매수 가격 통과", slot
            ), "매수 신호"
        return None, "다음 가격선 대기"
    if len(bars) < max(spec.slow, spec.rsi_period) + 2:
        return None, "신호 계산용 완성 봉 부족"
    if (quote.at - bars[-1].at).total_seconds() > spec.timeframe * 120:
        return None, "최신 완성 봉 수집 대기"
    stamp = bars[-1].at.isoformat()
    if state.get("last_bar") == stamp:
        return None, "다음 완성 봉 대기"
    state["last_bar"] = stamp
    closes = [b.close for b in bars]
    fast, slow = ema(closes, spec.fast), ema(closes, spec.slow)
    holding = D(state["quantity"]) > 0
    if spec.kind == "trend":
        buy = fast[-2] <= slow[-2] and fast[-1] > slow[-1]
        sell = fast[-2] >= slow[-2] and fast[-1] < slow[-1]
        reason = "단기·장기 지수이동평균 교차"
    else:
        values = rsi(closes, spec.rsi_period)
        buy = values[-2] <= spec.rsi_entry < values[-1] and closes[-1] >= slow[-1]
        sell = values[-1] >= spec.rsi_exit or closes[-1] < slow[-1]
        reason = "RSI 과매도 회복·추세 확인" if not holding else "RSI 회복 완료 또는 추세 이탈"
    if holding and sell:
        if not order_size_valid(D(state["quantity"]), tick(quote.bid, spec.venue), spec.venue):
            return None, "보유 잔량의 최소 주문 금액 충족 대기"
        return Decision("SELL", D(state["quantity"]), tick(quote.bid, spec.venue), reason), "매도 신호"
    if not holding and buy:
        quantity = round_quantity(D(state["cash"]) * D("0.98") / quote.ask, spec.venue)
        if order_size_valid(quantity, quote.ask, spec.venue):
            return Decision("BUY", quantity, tick(quote.ask, spec.venue), reason), "매수 신호"
    return None, "매매 조건 대기"


def maker_grid_decision(spec, state, quote, blocked_slots):
    prices = levels(spec)
    for i in range(spec.grids):
        if i in blocked_slots:
            continue
        lot = state["lots"][str(i)]
        held = D(lot["quantity"])
        if order_size_valid(held, prices[i + 1], spec.venue):
            if prices[i + 1] > quote.bid:
                return Decision(
                    "SELL", held, prices[i + 1], "메이커 그리드 매도 대기 주문", i
                ), "매도 대기 주문 준비"
            continue
        size = min(
            grid_quantity(spec, i) - held,
            round_quantity(D(lot["cash"]) / (prices[i] * (1 + spec.commission_rate)), spec.venue),
        )
        if prices[i] < quote.ask and order_size_valid(size, prices[i], spec.venue):
            return Decision(
                "BUY", size, prices[i], "매도대금으로 메이커 재매수 대기 주문", i
            ), "재매수 대기 주문 준비"
    return None, "메이커 가격·기존 주문 체결 대기"


def inventory_decision(spec: StrategySpec, state: dict, quote: Quote):
    """Each pair recycles its own proceeds, including partial fills across restarts."""
    prices = levels(spec)
    for i in range(spec.grids):
        lot = state["lots"][str(i)]
        quantity = D(lot["quantity"])
        if quote.bid >= prices[i + 1] and order_size_valid(quantity, prices[i + 1], spec.venue):
            return Decision(
                "SELL", quantity, prices[i + 1], "보유 코인 그리드 매도 가격 도달", i
            ), "매도 신호"
    for i in reversed(range(spec.grids)):
        lot = state["lots"][str(i)]
        missing = grid_quantity(spec, i) - D(lot["quantity"])
        if missing <= 0 or quote.ask > prices[i]:
            continue
        size = min(
            missing, round_quantity(D(lot["cash"]) / (prices[i] * (1 + spec.commission_rate)), spec.venue)
        )
        if order_size_valid(size, prices[i], spec.venue):
            return Decision("BUY", size, prices[i], "해당 단계의 매도대금으로 재매수", i), "재매수 신호"
    return None, "보유 코인 매도·재매수 가격 대기"


def apply_fill(state: dict, side: str, quantity: Decimal, amount: Decimal, costs: Decimal, slot: int | None):
    owned, basis = D(state["quantity"]), D(state["cost_basis"])
    cash, realized = D(state["cash"]), D(state["realized"])
    if quantity < 0 or amount < 0:
        raise ValueError("누적 체결 수량 또는 금액이 감소했습니다")
    lots = {k: dict(v) for k, v in state["lots"].items()}
    lot = lots.get(str(slot), {"quantity": "0", "cost": "0"}) if slot is not None else None
    recycled = None
    if lot is not None and "cash" in lot:
        recycled = D(lot["cash"]) + (amount - costs if side == "SELL" else -amount - costs)
        if recycled < 0:
            raise ValueError("해당 단계의 매도대금보다 큰 재매수 체결입니다")
    if side == "BUY":
        cash -= amount + costs
        owned += quantity
        basis += amount
        if lot is not None:
            lot = {"quantity": str(D(lot["quantity"]) + quantity), "cost": str(D(lot["cost"]) + amount)}
    else:
        if quantity > owned or (lot is not None and quantity > D(lot["quantity"])):
            raise ValueError("봇 보유 수량보다 많은 매도 체결입니다")
        removed = ZERO
        if quantity:
            removed = D(lot["cost"]) * quantity / D(lot["quantity"]) if lot else basis * quantity / owned
        cash += amount - costs
        owned -= quantity
        basis -= removed
        realized += amount - removed
        if lot is not None:
            lot = {"quantity": str(D(lot["quantity"]) - quantity), "cost": str(D(lot["cost"]) - removed)}
    if lot is not None:
        if recycled is not None:
            lot["cash"] = str(recycled)
        lots[str(slot)] = lot
    state.update(
        cash=str(cash),
        quantity=str(owned),
        cost_basis=str(basis),
        realized=str(realized),
        costs=str(D(state["costs"]) + costs),
        lots=lots,
    )


def equity(state: dict, mark: Decimal) -> Decimal:
    return D(state["cash"]) + D(state["quantity"]) * mark
