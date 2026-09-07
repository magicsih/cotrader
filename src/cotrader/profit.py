"""Read-only cumulative trading profit, with explicit currency conversion."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from cotrader.broker import BrokerError
from cotrader.markets import VENUES, currency, portfolio_key
from cotrader.models import RuntimeState
from cotrader.services import put_runtime

ReportCurrency = Literal["KRW", "USD", "USDT"]
LABELS = {"toss": "토스증권", "upbit": "업비트 KRW", "upbit_usdt": "업비트 USDT"}
METRICS = ("total_net", "realized_net", "unrealized", "realized_gross", "costs")
STATUS_LABELS = {
    "READY": "정상",
    "MISSING": "집계 대기",
    "STALE": "갱신 필요",
    "INCOMPLETE": "평가 확인 필요",
    "UNRESOLVED": "주문 대조 필요",
    "INCONSISTENT": "원장 대조 필요",
}


def amount(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def timestamp(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        return result.replace(tzinfo=result.tzinfo or UTC)
    except (ValueError, TypeError):
        return None


def current_fx(data, at):
    start, end, checked = (timestamp(data.get(key)) for key in ("valid_from", "valid_until", "checked_at"))
    rate = amount(data.get("rate"))
    return bool(
        data.get("status") == "CONNECTED"
        and rate is not None
        and rate > 0
        and start
        and end
        and checked
        and start <= at < end
        and -5 <= (at - checked).total_seconds() <= 120
    )


def build_summary(portfolios, fx, mode="live", base_currency: ReportCurrency = "KRW", at=None):
    at = at or datetime.now(UTC)
    rates = {"KRW": Decimal(1)}
    conversions = []
    for unit in ("USD", "USDT"):
        data = fx[unit].data if fx.get(unit) else {}
        valid = current_fx(data, at)
        if valid:
            rates[unit] = amount(data["rate"])
        conversions.append(
            {
                "currency": unit,
                "quote_currency": "KRW",
                "rate": str(rates[unit]) if valid else None,
                "source": data.get("source"),
                "checked_at": data.get("checked_at"),
                "valid_until": data.get("valid_until"),
            }
        )
    markets, issues = [], []
    for venue in VENUES:
        row = portfolios.get(venue)
        data = row.data if row else {}
        checked = timestamp(row.updated_at) if row else None
        gross, costs, unrealized = (
            amount(data.get(key)) for key in ("realized_gross", "costs", "unrealized")
        )
        realized_net = gross - costs if gross is not None and costs is not None else None
        total = realized_net + unrealized if realized_net is not None and unrealized is not None else None
        equity, capital = (amount(data.get(key)) for key in ("equity", "capital"))
        status = "READY"
        if row is None:
            status = "MISSING"
        elif not checked or not -5 <= (at - checked).total_seconds() <= 120:
            status = "STALE"
        elif not data.get("complete") or total is None or equity is None or capital is None:
            status = "INCOMPLETE"
        elif data.get("unresolved_orders"):
            status = "UNRESOLVED"
        elif abs(total - (equity - capital)) > Decimal("0.000001"):
            status = "INCONSISTENT"
        values = dict(zip(METRICS, (total, realized_net, unrealized, gross, costs), strict=True))
        unit = currency(venue)
        converted, factor = None, None
        if status == "READY":
            if unit == base_currency:
                factor = Decimal(1)
            elif unit in rates and base_currency in rates:
                factor = rates[unit] / rates[base_currency]
            if factor is not None:
                converted = {key: str(value * factor) for key, value in values.items()}
            elif all(value == 0 for value in values.values()):
                converted = {key: "0" for key in METRICS}
            else:
                issues.append(f"{LABELS[venue]} · {unit}→{base_currency} 환율 확인 필요")
        else:
            issues.append(f"{LABELS[venue]} · {STATUS_LABELS[status]}")
        markets.append(
            {
                "venue": venue,
                "label": LABELS[venue],
                "currency": unit,
                "status": status,
                "status_label": STATUS_LABELS[status],
                "checked_at": checked.isoformat() if checked else None,
                "metrics": {key: str(value) if value is not None else None for key, value in values.items()},
                "conversion_rate": str(factor) if factor is not None else None,
                "converted": converted,
            }
        )
    complete = all(row["converted"] is not None for row in markets)
    return {
        "mode": mode,
        "base_currency": base_currency,
        "complete": complete,
        "at": at.isoformat(),
        "markets": markets,
        "fx": conversions,
        "issues": issues,
        "total": {
            key: str(sum((Decimal(row["converted"][key]) for row in markets), Decimal(0)))
            if complete
            else None
            for key in METRICS
        },
    }


async def profit_summary(session, mode="live", base_currency: ReportCurrency = "KRW"):
    keys = [portfolio_key(venue, mode) for venue in VENUES] + ["fx:USD:KRW", "fx:USDT:KRW"]
    rows = {
        row.key: row for row in await session.scalars(select(RuntimeState).where(RuntimeState.key.in_(keys)))
    }
    return build_summary(
        {venue: rows.get(portfolio_key(venue, mode)) for venue in VENUES},
        {unit: rows.get(f"fx:{unit}:KRW") for unit in ("USD", "USDT")},
        mode,
        base_currency,
    )


async def fetch_fx(broker, upbit, unit):
    if unit == "USD":
        raw = await broker.request(
            "GET",
            "/api/v1/exchange-rate",
            group="MARKET_INFO",
            params={"baseCurrency": "USD", "quoteCurrency": "KRW"},
        )
        if raw["baseCurrency"] != "USD" or raw["quoteCurrency"] != "KRW":
            raise ValueError("Unexpected exchange rate direction")
        data = {
            "rate": raw["midRate"],
            "valid_from": raw["validFrom"],
            "valid_until": raw["validUntil"],
            "source": "토스 USD/KRW 매매기준율",
        }
    else:
        (quote,) = await upbit.orderbooks(["KRW-USDT"])
        if quote.symbol != "KRW-USDT" or quote.bid <= 0 or quote.ask < quote.bid:
            raise ValueError("Unexpected USDT quote")
        data = {
            "rate": str(quote.mid),
            "valid_from": quote.at.isoformat(),
            "valid_until": (quote.at + timedelta(seconds=120)).isoformat(),
            "source": "업비트 KRW-USDT 매수·매도 중간가",
        }
    data.update(status="CONNECTED", checked_at=datetime.now(UTC).isoformat())
    if not current_fx(data, datetime.now(UTC)):
        raise ValueError("Invalid or expired exchange rate")
    return data


async def refresh_fx(settings, broker, upbit, sessions):
    for unit, enabled in (("USD", settings.market_source == "toss"), ("USDT", settings.upbit_enabled)):
        try:
            data = await fetch_fx(broker, upbit, unit) if enabled else {"status": "DISCONNECTED"}
        except (BrokerError, ValueError, KeyError, TypeError, ArithmeticError):
            data = {"status": "ERROR"}
        async with sessions.begin() as session:
            previous = await session.get(RuntimeState, f"fx:{unit}:KRW")
            await put_runtime(session, f"fx:{unit}:KRW", {**(previous.data if previous else {}), **data})


async def fx_loop(settings, broker, upbit, sessions):
    # Reuse the engine's broker and token; reporting requests never block its order cycle.
    while True:
        try:
            await refresh_fx(settings, broker, upbit, sessions)
        except SQLAlchemyError:
            logging.getLogger(__name__).warning("profit exchange rate cache unavailable")
        await asyncio.sleep(30)
