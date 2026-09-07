"""Live-order checks shared by readiness and dispatch. No order submission here."""

from decimal import ROUND_CEILING, ROUND_DOWN
from typing import Literal

from pydantic import BaseModel, Field

from cotrader.domain import D, StrategySpec, levels
from cotrader.markets import UpbitCaution, currency, price_tick, round_quantity, tick_size, upbit_venue


class InventoryGridRequest(BaseModel):
    symbol: str
    allocation: Literal["all"]
    first_sell_basis: Literal["average", "market", "near_market"]
    execution_policy: Literal["trigger_limit", "maker_only"] = "trigger_limit"
    allowed_market_cautions: tuple[UpbitCaution, ...] = ()
    source_strategy_id: str | None = Field(default=None, max_length=36)
    source_version: int | None = None
    source_approval: str | None = Field(default=None, max_length=16)
    grids: int = Field(default=5, ge=2, le=30)
    step_percent: D = Field(default=D("2"), ge=D("0.5"), le=D("10"))


class MarketCautionsRequest(BaseModel):
    model_config = {"extra": "forbid"}

    strategy_id: str = Field(min_length=1, max_length=36)
    version: int = Field(ge=1)
    approval: str = Field(pattern=r"^[a-f0-9]{16}$")
    allowed_market_cautions: tuple[UpbitCaution, ...]


def inventory_grid_spec(request, chance, quote):
    venue = upbit_venue(request.symbol)
    maker = request.execution_policy == "maker_only"
    fee = max(
        amount(chance[k])
        for k in (
            ("maker_bid_fee", "maker_ask_fee")
            if maker
            else ("bid_fee", "ask_fee", "maker_bid_fee", "maker_ask_fee")
        )
    )
    commission = max(fee, D("0.0025") if venue == "upbit_usdt" else D("0.001"))
    _, available, total = upbit_policy(chance, request.symbol, commission, maker_only=maker)
    if not available or available != total or available != round_quantity(available, venue):
        raise ValueError("매도 가능 전량이 필요합니다. 잠긴 수량·소수점 자릿수·미체결을 확인하세요")
    average = amount(chance["ask_account"]["avg_buy_price"])
    average_currency = chance["ask_account"].get("unit_currency", "KRW")
    ratio = 1 + request.step_percent / 100
    target = quote.ask * ratio
    if request.first_sell_basis == "near_market":
        target = quote.ask + tick_size(quote.ask, venue)
    if request.first_sell_basis == "average":
        if average_currency != currency(venue):
            raise ValueError("계좌 평균가는 다른 통화입니다. 현재 호가 기준으로 준비하세요")
        if not average:
            raise ValueError("평균 매수가를 확인할 수 없습니다. 현재가 기준을 선택하세요")
        target = max(target, average * (1 + commission) / (1 - commission))
    target = price_tick(target, venue, ROUND_CEILING)
    lower = price_tick(target / ratio, venue, ROUND_CEILING)
    values = dict(
        venue=venue,
        symbol=request.symbol,
        kind="grid",
        budget=available * quote.bid,
        inventory_quantity=available,
        inventory_reference_price=quote.bid,
        inventory_average_price=average,
        inventory_average_currency=average_currency,
        execution_policy=request.execution_policy,
        allowed_market_cautions=request.allowed_market_cautions,
        lower=lower,
        upper=price_tick(lower * ratio**request.grids, venue, ROUND_CEILING),
        grids=request.grids,
        spacing="geometric",
        commission_rate=commission,
    )
    if request.first_sell_basis == "near_market":
        # Choose legal bounds whose rounded first sell is exactly one tick above ask.
        for low_round in (ROUND_CEILING, ROUND_DOWN):
            for high_round in (ROUND_CEILING, ROUND_DOWN):
                low = price_tick(target / ratio, venue, low_round)
                try:
                    candidate = StrategySpec(
                        **{
                            **values,
                            "lower": low,
                            "upper": price_tick(low * ratio**request.grids, venue, high_round),
                        }
                    )
                except ValueError:
                    continue
                if levels(candidate)[1] == target:
                    return candidate
        raise ValueError("한 틱 시작가와 간격을 호가 단위로 구성할 수 없습니다. 간격을 조정하세요")
    return StrategySpec(**values)


def amount(value):
    number = D(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("계좌 응답의 금액·수량을 확인할 수 없습니다")
    return number


def pending_order_matches(intent, order):
    """Read-only evidence check; never reconcile or invent missing execution data."""
    try:
        execution = order["execution"]
        return (
            bool(intent.broker_id)
            and intent.costs_final
            and intent.status in {"PENDING", "PARTIAL_FILLED"}
            and (
                order["orderId"],
                order["clientOrderId"],
                order["symbol"],
                order["side"],
                amount(order["quantity"]),
                amount(order["price"]),
                order["status"],
                amount(execution["filledQuantity"]),
                amount(execution["filledAmount"]),
                amount(execution["commission"]) + amount(execution["tax"]),
            )
            == (
                intent.broker_id,
                intent.id,
                intent.symbol,
                intent.side,
                intent.quantity,
                intent.price,
                intent.status,
                intent.filled_quantity,
                intent.filled_amount,
                intent.costs,
            )
        )
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False


def upbit_policy(chance, symbol, commission_rate, *, side=None, total=None, maker_only=False):
    market = chance["market"]
    venue = upbit_venue(symbol)
    coin = symbol.split("-", 1)[1]
    if (
        market["id"] != symbol
        or market["state"] != "active"
        or chance["bid_account"]["currency"] != currency(venue)
        or chance["ask_account"]["currency"] != coin
    ):
        raise ValueError("업비트 주문 가능 시장·통화 확인 필요")
    for direction in [side] if side else ["bid", "ask"]:
        if direction not in market["order_sides"] or "limit" not in market[f"{direction}_types"]:
            raise ValueError("업비트 지정가 매수·매도 지원 확인 필요")
        fee = (
            amount(chance[f"maker_{direction}_fee"])
            if maker_only
            else max(amount(chance[f"{direction}_fee"]), amount(chance[f"maker_{direction}_fee"]))
        )
        if fee > commission_rate:
            raise ValueError("실제 계좌 수수료가 전략의 비용 가정보다 높습니다")
        if total is not None and not amount(market[direction]["min_total"]) <= total <= amount(
            market["max_total"]
        ):
            raise ValueError("업비트 최소·최대 주문 금액을 벗어났습니다")
    return (
        amount(chance["bid_account"]["balance"]),
        amount(chance["ask_account"]["balance"]),
        amount(chance["ask_account"]["balance"]) + amount(chance["ask_account"]["locked"]),
    )
