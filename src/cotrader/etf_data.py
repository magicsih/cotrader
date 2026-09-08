"""Public daily signal data. Execution prices always come from Toss quotes."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import exchange_calendars
import httpx

from cotrader.domain import ETF_UNIVERSE, D
from cotrader.rotation import LOOKBACK, NEW_YORK


@lru_cache(maxsize=4)
def calendar(year):
    return exchange_calendars.get_calendar("XNYS", start=f"{year - 3}-01-01", end=f"{year + 1}-12-31")


def last_completed_session(at):
    market = calendar(at.year)
    day = market.date_to_session(at.astimezone(NEW_YORK).date().isoformat(), direction="previous")
    if market.session_close(day).to_pydatetime() + timedelta(minutes=5) > at:
        day = market.previous_session(day)
    return day.date().isoformat()


def parse_history(symbol, document, at):
    chart = document["chart"]
    if chart.get("error") or not chart.get("result") or len(chart["result"]) != 1:
        raise ValueError(f"{symbol} 일봉 조회 실패")
    result = chart["result"][0]
    meta = result["meta"]
    if meta.get("symbol") != symbol or meta.get("currency") != "USD" or meta.get("instrumentType") != "ETF":
        raise ValueError(f"{symbol} 일봉 종목·통화·ETF 구분 불일치")
    quotes = result["indicators"]["quote"][0]
    timestamps = result["timestamp"]
    if len(timestamps) != len(quotes["close"]) or len(timestamps) != len(quotes["volume"]):
        raise ValueError(f"{symbol} 일봉 응답 길이 불일치")
    events = result.get("events", {})
    dividends = {}
    for value in events.get("dividends", {}).values():
        day = datetime.fromtimestamp(value["date"], UTC).astimezone(NEW_YORK).date().isoformat()
        dividend = D(str(value["amount"]))
        if not dividend.is_finite() or dividend < 0 or day in dividends:
            raise ValueError(f"{symbol} 배당 정보가 유효하지 않습니다")
        dividends[day] = dividend
    last = last_completed_session(at)
    rows = []
    for index, stamp in enumerate(timestamps):
        day = datetime.fromtimestamp(stamp, UTC).astimezone(NEW_YORK).date().isoformat()
        if day > last:
            continue
        close = D(str(quotes["close"][index]))
        volume = D(str(quotes["volume"][index]))
        if not close.is_finite() or close <= 0 or not volume.is_finite() or volume <= 0:
            raise ValueError(f"{symbol} {day} 종가 또는 거래량 누락")
        rows.append({"date": day, "close": str(close), "dividend": str(dividends.get(day, D(0)))})
    dates = [r["date"] for r in rows]
    if len(rows) < LOOKBACK + 35 or dates != sorted(set(dates)) or dates[-1] != last:
        raise ValueError(f"{symbol} 최신 완성 일봉 또는 월초 순위 준비 자료 부족")
    expected = [s.date().isoformat() for s in calendar(at.year).sessions_in_range(dates[0], last)]
    if dates != expected:
        raise ValueError(f"{symbol} 정규장 거래일 누락 또는 달력 불일치")
    if any(
        dates[0] <= datetime.fromtimestamp(v["date"], UTC).astimezone(NEW_YORK).date().isoformat() <= last
        for v in events.get("splits", {}).values()
    ):
        raise ValueError(f"{symbol} 조회 기간의 분할 조정 대조가 필요합니다")
    total = D(1)
    previous = None
    for row in rows:
        close = D(row["close"])
        if previous is not None:
            total *= (close + D(row["dividend"])) / previous
        row["total_return"] = str(total)
        previous = close
    return rows


async def fetch_history(at, client=None):
    owned = client is None
    client = client or httpx.AsyncClient(timeout=15, headers={"User-Agent": "Cotrader/0.1"})
    series, hashes, distributions = {}, {}, {}
    try:
        for symbol in ETF_UNIVERSE:
            response = await client.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                params={"range": "2y", "interval": "1d", "events": "div,splits"},
            )
            response.raise_for_status()
            document = response.json()
            series[symbol] = parse_history(symbol, document, at)
            events = document["chart"]["result"][0].get("events", {})
            today = at.astimezone(NEW_YORK).date().isoformat()
            distributions[symbol] = []
            for value in events.get("dividends", {}).values():
                day = datetime.fromtimestamp(value["date"], UTC).astimezone(NEW_YORK).date().isoformat()
                if day <= today:
                    distributions[symbol].append({"date": day, "dividend": str(value["amount"])})
            if any(
                series[symbol][-1]["date"]
                < datetime.fromtimestamp(v["date"], UTC).astimezone(NEW_YORK).date().isoformat()
                <= today
                for v in events.get("splits", {}).values()
            ):
                raise ValueError(f"{symbol} 당일 분할 조정 확인 필요")
            hashes[symbol] = hashlib.sha256(response.content).hexdigest()
        dates = [r["date"] for r in series[ETF_UNIVERSE[0]]]
        if any([r["date"] for r in series[s]] != dates for s in ETF_UNIVERSE):
            raise ValueError("ETF별 일봉 조회 기간이 다릅니다")
        return {
            "series": series,
            "distributions": distributions,
            "source": "Yahoo Finance public chart",
            "checked_at": at.isoformat(),
            "last_session": dates[-1],
            "sha256": hashes,
            "fingerprint": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
        }
    finally:
        if owned:
            await client.aclose()
