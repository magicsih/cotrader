from datetime import UTC, datetime, timedelta

import pytest

from cotrader.broker import current_session, parse_quote
from cotrader.domain import (
    Bar,
    D,
    Quote,
    StrategySpec,
    aggregate,
    apply_fill,
    decide,
    ema,
    equity,
    initial_state,
    levels,
    rsi,
)
from cotrader.research import backtest

AT = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)


def grid(**overrides):
    return StrategySpec(
        symbol="TEST", budget="1000", lower="90", upper="110", grids=4, spacing="arithmetic", **overrides
    )


def quote(price, seconds=0):
    return Quote("TEST", D(price), D(price), AT + timedelta(seconds=seconds), D(10000), D(10000))


def bar(minute, close, low=None, high=None):
    price = D(close)
    return Bar(
        AT + timedelta(minutes=minute),
        price,
        D(high) if high else price + 1,
        D(low) if low else price - 1,
        price,
        D(10000),
    )


def test_grid_price_lines_and_affordable_integer_orders():
    assert levels(grid()) == [D(90), D(95), D(100), D(105), D(110)]
    with pytest.raises(ValueError, match="최소 1주"):
        StrategySpec(symbol="TEST", budget="100", lower="90", upper="110", grids=5)


def test_signal_strategy_waits_when_latest_candles_are_stale():
    spec = StrategySpec(symbol="TEST", kind="trend", budget="1000", timeframe=1, fast=2, slow=3, rsi_period=2)
    bars = [bar(i, str(100 + i)) for i in range(8)]
    decision, reason = decide(spec, initial_state(D(1000)), quote("108", seconds=15 * 60), bars)
    assert decision is None and reason == "최신 완성 봉 수집 대기"


def test_grid_does_not_buy_at_boot_and_matches_only_its_inventory():
    spec, state = grid(), initial_state(D(1000))
    assert decide(spec, state, quote("104"), [])[0] is None
    buy, _ = decide(spec, state, quote("94", 1), [])
    assert buy.side == "BUY" and buy.price == 95 and buy.slot == 1
    apply_fill(state, "BUY", D(2), D(190), D("0.19"), buy.slot)
    sell, _ = decide(spec, state, quote("101", 2), [])
    assert sell.side == "SELL" and sell.quantity == 2 and sell.price == 100
    apply_fill(state, "SELL", D(2), D(200), D("0.20"), sell.slot)
    assert equity(state, D(100)) == D("1009.61")
    assert D(state["quantity"]) == 0
    with pytest.raises(ValueError, match="보유 수량"):
        apply_fill(state, "SELL", D(1), D(100), D(0), None)


def test_lower_breach_never_adds_inventory():
    state = initial_state(D(1000))
    state["last_mid"] = "100"
    assert decide(grid(), state, quote("89"), []) == (None, "GRID_LOWER_BREACH")


def test_late_fee_correction_does_not_change_quantity():
    state = initial_state(D(1000))
    apply_fill(state, "BUY", D(2), D(200), D(1), 0)
    apply_fill(state, "BUY", D(0), D(0), D("-0.4"), 0)
    assert D(state["cash"]) == D("799.4")
    assert state["quantity"] == "2"


def test_indicators_and_closed_candles():
    assert ema([D(10), D(20), D(30)], 3) == [D(10), D(15), D("22.5")]
    assert rsi([D(10)] * 20, 14)[-1] == 50
    assert rsi([D(i) for i in range(1, 25)], 14)[-1] == 100
    bars = [bar(i, "100") for i in range(6)]
    assert len(aggregate(bars, 5, AT + timedelta(minutes=4))) == 0
    assert len(aggregate(bars, 5, AT + timedelta(minutes=5))) == 1
    assert len(aggregate([bars[0], *bars[2:]], 5, AT + timedelta(minutes=6))) == 0


def test_quotes_sort_price_levels_and_reject_missing_timestamp():
    raw = {
        "currency": "USD",
        "timestamp": AT.isoformat(),
        "asks": [{"price": "102", "volume": "10"}, {"price": "101", "volume": "4"}],
        "bids": [{"price": "99", "volume": "10"}, {"price": "100", "volume": "4"}],
    }
    result = parse_quote("TEST", raw)
    assert result.ask == 101 and result.bid == 100
    assert not result.valid(grid(), AT)  # Wide spread.
    assert not quote("100").valid(grid(), AT + timedelta(seconds=11))
    assert parse_quote("TEST", {**raw, "timestamp": None}) is None


def test_us_calendar_handles_korean_midnight_and_holidays():
    calendar = {
        "today": {
            "date": "2026-03-25",
            "regularMarket": {
                "startTime": "2026-03-25T22:30:00+09:00",
                "endTime": "2026-03-26T05:00:00+09:00",
            },
        }
    }
    assert current_session(calendar, datetime.fromisoformat("2026-03-26T01:00:00+09:00")) == (
        "regularMarket",
        "2026-03-25",
    )
    assert current_session(calendar, datetime.fromisoformat("2026-03-26T05:00:00+09:00")) is None
    assert current_session({"today": {"date": "2026-03-25", "regularMarket": None}}, AT) is None


def test_backtest_does_not_round_trip_inside_same_candle_and_includes_open_inventory():
    bars = [bar(0, "104"), bar(1, "94"), bar(2, "101", "92", "108"), bar(3, "104", "99", "109")]
    result = backtest(grid(), bars)
    assert [t["side"] for t in result["trades"]] == ["BUY", "SELL"]
    assert result["trades"][0]["at"] != result["trades"][1]["at"]
    assert D(result["costs"]) > 0
    partial = backtest(grid(), bars[:3])
    assert D(partial["held_quantity"]) > 0
    assert D(partial["unrealized"]) > 0


def test_backtest_prefix_is_unchanged_by_future_data():
    prefix = [bar(0, "104"), bar(1, "94"), bar(2, "101", "92", "108"), bar(3, "104", "99", "109")]
    short = backtest(grid(), prefix)
    long = backtest(grid(), prefix + [bar(4, "109"), bar(5, "103")])
    assert short["trades"] == [t for t in long["trades"] if t["at"] <= prefix[-1].at.isoformat()]
    assert len(short["data_hash"]) == 64
