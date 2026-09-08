from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from cotrader.api import create_app
from cotrader.config import Settings
from cotrader.domain import ETF_UNIVERSE, D, Quote
from cotrader.etf_data import calendar
from cotrader.models import Intent, Ledger, PaperEvent, PaperPortfolio
from cotrader.paper_lab import (
    CRYPTO_UNIVERSE,
    advance,
    bootstrap_lab,
    digest,
    distributions,
    policy,
    report,
    signal,
)
from cotrader.rotation import held

AT = datetime(2026, 9, 8, 14, tzinfo=UTC)


def settings(**options):
    return Settings(
        runtime_role="engine",
        paper_lab_enabled=True,
        paper_lab_usd="6000",
        paper_lab_usdt="8000",
        market_source="toss",
        upbit_enabled=True,
        **options,
    )


def history(venue="upbit_usdt"):
    if venue == "toss":
        days = [s.date().isoformat() for s in calendar(2026).sessions_in_range("2025-01-02", "2026-09-04")]
        symbols = ETF_UNIVERSE
    else:
        days = [(AT.date() - timedelta(days=i)).isoformat() for i in range(400, 0, -1)]
        symbols = CRYPTO_UNIVERSE
    series = {
        s: [
            {"date": day, "close": str(100 + i), "total_return": str(1 + D(i) / 100), "dividend": "0"}
            for i, day in enumerate(days)
        ]
        for s in symbols
    }
    return {"series": series, "fingerprint": digest(series)}


def quotes(at=AT, size="1000", price="499", venue="upbit_usdt"):
    return {
        s: Quote(s, D(price), D(price), at, D(size), D(size))
        for s in (ETF_UNIVERSE if venue == "toss" else CRYPTO_UNIVERSE)
    }


async def start(sessions):
    async with sessions.begin() as s:
        await bootstrap_lab(s, settings(), AT)


def test_live_flags_and_automatic_recovery_cannot_coexist_with_lab():
    for flag in ("live_enabled", "upbit_live_enabled", "upbit_usdt_live_enabled", "upbit_usdt_auto_recover"):
        with pytest.raises(ValueError, match="모의 운용"):
            settings(**{flag: True})


def test_signal_uses_completed_month_only_and_waits_for_gaps():
    h = history("toss")["series"]
    stamp, weights, details = signal(policy("toss", "base"), h, AT, {})
    assert stamp == "2026-08-31" and all(D(w) > 0 for w in weights.values())
    h["SPY"][-1]["total_return"] = "999999999"
    assert signal(policy("toss", "base"), h, AT, {}) == (stamp, weights, details)
    h["QQQ"].pop(-10)
    with pytest.raises(ValueError, match="날짜 불일치"):
        signal(policy("toss", "base"), h, AT, {})
    c = history()["series"]
    for rows in c.values():
        rows.pop(-10)
    with pytest.raises(ValueError, match="일봉 누락"):
        signal(policy("upbit_usdt", "base"), c, AT, {})


def test_buffer_retains_previous_target_inside_band():
    h = history()["series"]
    for rows in h.values():
        for r in rows:
            r["close"] = "100"
        rows[-1]["close"] = "100.5"
    _, entering, _ = signal(policy("upbit_usdt", "buffer"), h, AT, {})
    _, retaining, _ = signal(policy("upbit_usdt", "buffer"), h, AT, {s: "0.5" for s in CRYPTO_UNIVERSE})
    assert set(entering.values()) == {"0"} and set(retaining.values()) == {"0.5"}


async def test_independent_accounts_and_immutable_budget(db):
    _, sessions = db
    await start(sessions)
    async with sessions.begin() as s:
        await bootstrap_lab(s, settings(), AT)
        rows = (await s.scalars(select(PaperPortfolio))).all()
        assert len(rows) == 8
        assert [r.state["cash"] for r in rows if r.venue == "toss"] == ["6000"] * 4
        opts = settings()
        opts.paper_lab_usdt = D(9000)
        with pytest.raises(ValueError, match="변경할 수 없습니다"):
            await bootstrap_lab(s, opts, AT)


async def test_later_quote_partial_fill_restart_and_no_live_order_path(db):
    _, sessions = db
    await start(sessions)
    async with sessions.begin() as s:
        row = await s.get(PaperPortfolio, "upbit_usdt-base-v1")
        await advance(s, row, history(), quotes(size="1"), AT, True)
        assert row.state["action"] == "BUY" and not row.state.get("fill_count")
    async with sessions.begin() as s:
        row = await s.get(PaperPortfolio, "upbit_usdt-base-v1")
        await advance(s, row, history(), quotes(size="1"), AT + timedelta(seconds=1), True)
        assert not row.state.get("fill_count")
        later = AT + timedelta(seconds=2)
        await advance(s, row, history(), quotes(later, "1"), later, True)
        assert held(row.state, "USDT-BTC") == D("0.1")
        assert D(row.state["costs"]) == D("49.9") * D("0.001")
        cash = row.state["cash"]
    async with sessions.begin() as s:
        row = await s.get(PaperPortfolio, "upbit_usdt-base-v1")
        await advance(s, row, history(), quotes(later, "1"), later + timedelta(seconds=1), True)
        assert row.state["cash"] == cash and row.state["fill_count"] == 1
        other = await s.get(PaperPortfolio, "upbit_usdt-buffer-v1")
        assert other.state["cash"] == "8000"
        assert await s.scalar(select(func.count()).select_from(Intent)) == 0
        assert await s.scalar(select(func.count()).select_from(Ledger)) == 0


async def test_expiry_and_data_outage_cancel_without_fake_fill(db):
    _, sessions = db
    await start(sessions)
    async with sessions.begin() as s:
        row = await s.get(PaperPortfolio, "upbit_usdt-base-v1")
        await advance(s, row, history(), quotes(), AT, True)
        await advance(s, row, history(), quotes(), AT + timedelta(seconds=60), True)
        assert not row.state.get("pending") and not row.state.get("fill_count")
        await advance(s, row, None, {}, AT + timedelta(seconds=121), True)
        assert row.state["action"] == "WAIT" and row.state["cash"] == "8000"


async def test_closed_etf_session_never_opens_order(db):
    _, sessions = db
    await start(sessions)
    async with sessions.begin() as s:
        row = await s.get(PaperPortfolio, "toss-base-v1")
        await advance(s, row, history("toss"), quotes(venue="toss"), AT, False)
        assert row.state["action"] == "WAIT" and not row.state.get("pending")
        assert "정규장" in row.state["reason"]


async def test_trend_exit_sells_existing_quantity_and_preserves_other_account(db):
    _, sessions = db
    await start(sessions)
    async with sessions.begin() as s:
        row = await s.get(PaperPortfolio, "upbit_usdt-base-v1")
        row.state = {
            **row.state,
            "cash": "7000",
            "positions": {"USDT-BTC": {"quantity": "2", "cost_basis": "1000", "realized": "0", "costs": "0"}},
        }
        h = history()
        for bars in h["series"].values():
            bars[-1]["close"] = "100"
        await advance(s, row, h, quotes(price="100"), AT, True)
        assert row.state["pending"]["side"] == "SELL"
        assert row.state["pending"]["quantity"] == "2"
        later = AT + timedelta(seconds=2)
        await advance(s, row, h, quotes(later, price="100"), later, True)
        assert held(row.state, "USDT-BTC") == 0
        assert D(row.state["cash"]) == D("7199.8")
        assert D(row.state["realized"]) - D(row.state["costs"]) == D("-800.2")
        other = await s.get(PaperPortfolio, "upbit_usdt-buffer-v1")
        assert other.state["cash"] == "8000"
        assert row.state["action"] == "HOLD"


async def test_stale_quotes_never_create_order_or_overwrite_held_valuation(db):
    _, sessions = db
    await start(sessions)
    async with sessions.begin() as s:
        row = await s.get(PaperPortfolio, "upbit_usdt-base-v1")
        row.state = {
            **row.state,
            "cash": "7501",
            "nav": "8000",
            "nav_at": AT.isoformat(),
            "positions": {"USDT-BTC": {"quantity": "1", "cost_basis": "499", "realized": "0", "costs": "0"}},
        }
        await advance(s, row, history(), quotes(AT - timedelta(seconds=30)), AT, True)
        assert row.state["action"] == "WAIT" and not row.state.get("pending")
        assert row.state["nav"] == "8000" and row.state["nav_at"] == AT.isoformat()


async def test_dividend_entitlement_uses_pre_ex_date_fills_even_if_sold(db):
    _, sessions = db
    await start(sessions)
    async with sessions.begin() as s:
        row = await s.get(PaperPortfolio, "toss-base-v1")
        row.created_at = datetime(2026, 9, 1)
        for index, (side, stamp) in enumerate(
            (("BUY", datetime(2026, 9, 2, 15)), ("SELL", datetime(2026, 9, 3, 15)))
        ):
            s.add(
                PaperEvent(
                    portfolio_id=row.id,
                    kind="fill",
                    fingerprint=f"example-{index}",
                    data={"symbol": "SPY", "quantity": "3", "side": side, "order_id": f"o{index}"},
                    created_at=stamp,
                )
            )
        await s.flush()
        state = deepcopy(row.state)
        series = {"SPY": [{"date": "2026-09-03", "dividend": "2"}]}
        await distributions(s, row, state, series, AT)
        assert state["dividend_receivable"] == "6" and state["cash"] == row.state["cash"]
        await distributions(s, row, state, series, AT)
        await s.flush()
        assert (
            await s.scalar(select(func.count()).select_from(PaperEvent).where(PaperEvent.kind == "dividend"))
            == 1
        )


async def test_paper_api_requires_auth_and_insufficient_evidence_is_explicit(db):
    _, sessions = db
    await start(sessions)
    app = create_app(Settings(), sessions_override=sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)), base_url="http://127.0.0.1:8000"
    ) as client:
        response = await client.get("/api/paper-lab")
        assert response.status_code == 200
        body = response.json()
        assert len(body["portfolios"]) == 8 and not body["actual_order_created"]
        assert all(e["status"] == "KEEP_COLLECTING" for e in body["evaluations"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("10.1.1.1", 5000)), base_url="http://example.com"
    ) as client:
        assert (await client.get("/api/paper-lab")).status_code == 403
    async with sessions() as s:
        result = await report(s, AT + timedelta(days=180))
        assert all(e["status"] == "KEEP_COLLECTING" for e in result["evaluations"])
