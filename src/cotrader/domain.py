from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

D = Decimal
ZERO = D(0)
ACTIVE_ORDERS = {"PREPARED", "SENDING", "UNKNOWN", "PENDING", "PARTIAL_FILLED", "PENDING_CANCEL"}


class StrategySpec(BaseModel):
    symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.\-]{0,23}$")
    kind: Literal["grid", "trend", "rebound"] = "grid"
    budget: Decimal = Field(gt=0, le=5000)
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
        if self.fast >= self.slow:
            raise ValueError("단기 평균 기간은 장기 평균 기간보다 작아야 합니다")
        if self.kind == "grid":
            if self.lower is None or self.upper is None or self.lower >= self.upper:
                raise ValueError("그리드 하단과 상단을 올바르게 입력하세요")
            prices = levels(self)
            if len(set(prices)) != len(prices):
                raise ValueError("호가 단위 반올림 후 가격선이 중복됩니다")
            if any(slot_quantity(self, p) < 1 for p in prices[:-1]):
                raise ValueError("각 가격선에 최소 1주가 필요합니다. 예산을 늘리거나 단계 수를 줄이세요")
            minimum_gap = min((b - a) / a for a, b in zip(prices, prices[1:], strict=False))
            if minimum_gap <= self.commission_rate * 2 + self.slippage_bps / D(10000) * 2:
                raise ValueError("그리드 간격이 왕복 수수료와 가정한 체결 비용보다 좁습니다")
        return self


def tick(price: Decimal) -> Decimal:
    return price.quantize(D("0.0001") if price < 1 else D("0.01"), rounding=ROUND_DOWN)


def levels(spec: StrategySpec) -> list[Decimal]:
    if spec.spacing == "arithmetic":
        return [
            tick(spec.lower + (spec.upper - spec.lower) * D(i) / spec.grids) for i in range(spec.grids + 1)
        ]
    ratio = (spec.upper / spec.lower) ** (D(1) / spec.grids)
    return [tick(spec.lower * ratio**i) for i in range(spec.grids)] + [tick(spec.upper)]


def slot_quantity(spec: StrategySpec, price: Decimal) -> Decimal:
    return (spec.budget * D("0.98") / spec.grids / price).to_integral_value(rounding=ROUND_DOWN)


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
        return bool(self.bid > 0 and self.ask >= self.bid and 0 <= age <= spec.quote_max_age)

    def valid(self, spec: StrategySpec, at: datetime) -> bool:
        return self.fresh(spec, at) and (self.ask - self.bid) / self.mid * 10000 <= spec.max_spread_bps


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


def decide(spec: StrategySpec, state: dict, quote: Quote, bars: list[Bar]) -> tuple[Decision | None, str]:
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
        return Decision("SELL", D(state["quantity"]), tick(quote.bid), reason), "매도 신호"
    if not holding and buy:
        quantity = (D(state["cash"]) * D("0.98") / quote.ask).to_integral_value(rounding=ROUND_DOWN)
        if quantity >= 1:
            return Decision("BUY", quantity, tick(quote.ask), reason), "매수 신호"
    return None, "매매 조건 대기"


def apply_fill(state: dict, side: str, quantity: Decimal, amount: Decimal, costs: Decimal, slot: int | None):
    owned, basis = D(state["quantity"]), D(state["cost_basis"])
    cash, realized = D(state["cash"]), D(state["realized"])
    if quantity < 0 or amount < 0:
        raise ValueError("누적 체결 수량 또는 금액이 감소했습니다")
    lots = {k: dict(v) for k, v in state["lots"].items()}
    lot = lots.get(str(slot), {"quantity": "0", "cost": "0"}) if slot is not None else None
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
