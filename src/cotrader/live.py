"""Live-order checks shared by readiness and dispatch. No order submission here."""

from cotrader.domain import D


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
