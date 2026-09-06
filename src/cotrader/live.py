"""Live-order checks shared by readiness and dispatch. No order submission here."""

from decimal import ROUND_CEILING
from typing import Literal

from pydantic import BaseModel, Field

from cotrader.domain import D, StrategySpec
from cotrader.markets import price_tick, round_quantity, validate_symbol


class InventoryGridRequest(BaseModel):
    symbol: str
    allocation: Literal["all"]
    first_sell_basis: Literal["average", "market"]
    grids: int = Field(default=5, ge=2, le=30)
    step_percent: D = Field(default=D("2"), ge=D("0.5"), le=D("10"))


def inventory_grid_spec(request, chance, quote):
    validate_symbol("upbit", request.symbol)
    fee = max(amount(chance[k]) for k in ("bid_fee", "ask_fee", "maker_bid_fee", "maker_ask_fee"))
    commission = max(fee, D("0.001"))
    _, available, total = upbit_policy(chance, request.symbol, commission)
    if not available or available != total or available != round_quantity(available, "upbit"):
        raise ValueError("매도 가능 전량이 필요합니다. 잠긴 수량·소수점 자릿수·미체결을 확인하세요")
    average = amount(chance["ask_account"]["avg_buy_price"])
    ratio = 1 + request.step_percent / 100
    target = quote.ask * ratio
    if request.first_sell_basis == "average":
        if not average:
            raise ValueError("평균 매수가를 확인할 수 없습니다. 현재가 기준을 선택하세요")
        target = max(target, average * (1 + commission) / (1 - commission))
    target = price_tick(target, "upbit", ROUND_CEILING)
    lower = price_tick(target / ratio, "upbit", ROUND_CEILING)
    spec = StrategySpec(
        venue="upbit",
        symbol=request.symbol,
        kind="grid",
        budget=available * quote.bid,
        inventory_quantity=available,
        inventory_reference_price=quote.bid,
        inventory_average_price=average,
        lower=lower,
        upper=price_tick(lower * ratio**request.grids, "upbit", ROUND_CEILING),
        grids=request.grids,
        spacing="geometric",
        commission_rate=commission,
    )
    return spec


def amount(value):
    number = D(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("계좌 응답의 금액·수량을 확인할 수 없습니다")
    return number


def upbit_policy(chance, symbol, commission_rate, *, side=None, total=None):
    market = chance["market"]
    coin = symbol.removeprefix("KRW-")
    if (
        market["id"] != symbol
        or market["state"] != "active"
        or chance["bid_account"]["currency"] != "KRW"
        or chance["ask_account"]["currency"] != coin
    ):
        raise ValueError("업비트 주문 가능 시장·통화 확인 필요")
    for direction in [side] if side else ["bid", "ask"]:
        if direction not in market["order_sides"] or "limit" not in market[f"{direction}_types"]:
            raise ValueError("업비트 지정가 매수·매도 지원 확인 필요")
        fee = max(amount(chance[f"{direction}_fee"]), amount(chance[f"maker_{direction}_fee"]))
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
