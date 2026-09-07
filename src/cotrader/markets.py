import re
from decimal import ROUND_DOWN, Decimal
from typing import Literal

Venue = Literal["toss", "upbit", "upbit_usdt"]
UPBIT_VENUES = ("upbit", "upbit_usdt")
VENUES = ("toss", *UPBIT_VENUES)


def is_upbit(venue: str) -> bool:
    return venue in UPBIT_VENUES


def currency(venue: Venue) -> str:
    return {"toss": "USD", "upbit": "KRW", "upbit_usdt": "USDT"}[venue]


def upbit_venue(symbol: str) -> Venue:
    venue = {"KRW": "upbit", "USDT": "upbit_usdt"}.get(symbol.split("-", 1)[0])
    if venue is None:
        raise ValueError("업비트는 KRW 또는 USDT 거래쌍을 지원합니다")
    validate_symbol(venue, symbol)
    return venue


def asset_key(venue: Venue, symbol: str) -> str:
    if is_upbit(venue):
        if not re.fullmatch(r"(?:KRW|USDT|BTC)-[A-Z0-9]{1,16}", symbol):
            raise ValueError("업비트 거래쌍 확인 필요")
        return f"upbit:{symbol.split('-', 1)[1]}"
    validate_symbol(venue, symbol)
    return f"toss:{symbol}"


def validate_symbol(venue: Venue, symbol: str):
    if is_upbit(venue):
        valid = re.fullmatch(rf"{currency(venue)}-[A-Z0-9]{{1,16}}", symbol)
    else:
        valid = re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,23}", symbol) and not symbol.startswith(
            ("KRW-", "USDT-", "BTC-")
        )
    if not valid:
        raise ValueError("선택한 시장에 맞는 KRW-SOL, USDT-SOL 또는 미국 종목 코드를 입력하세요")


def round_quantity(value: Decimal, venue: Venue) -> Decimal:
    return value.quantize(Decimal("0.00000001") if is_upbit(venue) else Decimal(1), rounding=ROUND_DOWN)


def order_size_valid(quantity: Decimal, price: Decimal, venue: Venue) -> bool:
    return quantity > 0 and (
        quantity * price >= (Decimal("0.5") if venue == "upbit_usdt" else 5000)
        if is_upbit(venue)
        else quantity >= 1
    )


def tick_size(price: Decimal, venue: Venue) -> Decimal:
    if venue == "upbit_usdt":
        return next(
            (
                Decimal(step)
                for lower, step in (
                    ("10", ".01"),
                    ("1", ".001"),
                    (".1", ".0001"),
                    (".01", ".00001"),
                    (".001", ".000001"),
                    (".0001", ".0000001"),
                )
                if price >= Decimal(lower)
            ),
            Decimal(".00000001"),
        )
    if venue == "toss":
        return Decimal("0.0001") if price < 1 else Decimal("0.01")
    return next(
        (
            Decimal(step)
            for lower, step in (
                ("1000000", "1000"),
                ("500000", "500"),
                ("100000", "100"),
                ("50000", "50"),
                ("10000", "10"),
                ("5000", "5"),
                ("100", "1"),
                ("10", ".1"),
                ("1", ".01"),
                (".1", ".001"),
                (".01", ".0001"),
                (".001", ".00001"),
                (".0001", ".000001"),
                (".00001", ".0000001"),
            )
            if price >= Decimal(lower)
        ),
        Decimal(".00000001"),
    )


def price_tick(price: Decimal, venue: Venue, rounding=ROUND_DOWN) -> Decimal:
    step = tick_size(price, venue)
    return (price / step).to_integral_value(rounding=rounding) * step


def portfolio_key(venue: Venue, mode: str) -> str:
    return f"portfolio:{venue}:{mode}"
