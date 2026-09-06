import re
from decimal import ROUND_DOWN, Decimal
from typing import Literal

Venue = Literal["toss", "upbit"]
VENUES = ("toss", "upbit")


def currency(venue: Venue) -> str:
    return "KRW" if venue == "upbit" else "USD"


def validate_symbol(venue: Venue, symbol: str):
    if venue == "upbit":
        valid = re.fullmatch(r"KRW-[A-Z0-9]{1,16}", symbol)
    else:
        valid = re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,23}", symbol) and not symbol.startswith("KRW-")
    if not valid:
        raise ValueError("업비트는 KRW-BTC 같은 원화 거래쌍을, 토스는 AAPL 같은 미국 종목 코드를 입력하세요")


def round_quantity(value: Decimal, venue: Venue) -> Decimal:
    return value.quantize(Decimal("0.00000001") if venue == "upbit" else Decimal(1), rounding=ROUND_DOWN)


def order_size_valid(quantity: Decimal, price: Decimal, venue: Venue) -> bool:
    return quantity > 0 and (quantity * price >= 5000 if venue == "upbit" else quantity >= 1)


def price_tick(price: Decimal, venue: Venue) -> Decimal:
    if venue == "toss":
        step = Decimal("0.0001") if price < 1 else Decimal("0.01")
    else:
        # Upbit KRW policy effective 2025-07-31; current policy also applies to research.
        bands = [
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
        ]
        step = next((Decimal(s) for lower, s in bands if price >= Decimal(lower)), Decimal(".00000001"))
    return (price / step).to_integral_value(rounding=ROUND_DOWN) * step


def portfolio_key(venue: Venue, mode: str) -> str:
    return f"portfolio:{venue}:{mode}"
