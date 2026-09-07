from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from test_parallel_upbit_start import setup_pair
from test_usdt_maker import chance as usdt_chance
from test_usdt_maker import fixture, spec
from test_usdt_maker import quote as usdt_quote

from cotrader.domain import D, Quote, StrategySpec, funded_state
from cotrader.models import AccountState, Event, Intent, RuntimeState, Snapshot, Strategy
from cotrader.services import (
    LEGACY_RISK_HALT_REASON,
    RISK_RECOVERY_REASON,
    create_strategy,
)


def valuation(equity="950", *, pending=0, unresolved=0):
    return {
        "mode": "live",
        "venue": "upbit_usdt",
        "currency": "USDT",
        "capital": "1000",
        "equity": equity,
        "complete": True,
        "pending_orders": pending,
        "unresolved_orders": unresolved,
        "high_water": "1000",
        "daily_anchor": "1000",
        "halted": True,
        "reason": LEGACY_RISK_HALT_REASON,
        "daily_loss_limit": "80",
        "drawdown_limit": "400",
    }


async def halted_strategy(engine, *, reason=LEGACY_RISK_HALT_REASON):
    async with engine.sessions.begin() as session:
        row = await create_strategy(session, "자동 복구 SOL", spec(), "live")
        row.state = {**funded_state(spec()), "funded": True, "last_mid": "106", "last_bar": "old"}
        row.status, row.reason = "PAUSED", reason
        account = await session.get(AccountState, {"venue": "upbit_usdt", "mode": "live"})
        account.capital = account.high_water = account.daily_anchor = D(1000)
        account.halted, account.reason = True, LEGACY_RISK_HALT_REASON
        session.add(RuntimeState(key="portfolio:upbit_usdt:live", data=valuation()))
        session.add(
            Snapshot(
                venue="upbit_usdt",
                mode="live",
                data=valuation(),
                created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=61),
            )
        )
        await session.flush()
        return row.id


async def test_recovered_portfolio_auto_resumes_without_resetting_anchors_or_placing_order(db):
    engine = await fixture(db)
    engine.settings.upbit_usdt_auto_recover = True
    engine.settings.daily_loss_usdt = D(80)
    engine.settings.drawdown_usdt = D(400)
    strategy_id = await halted_strategy(engine)

    assert await engine.auto_recover_upbit_usdt(datetime.now(UTC))

    async with engine.sessions() as session:
        account = await session.get(AccountState, {"venue": "upbit_usdt", "mode": "live"})
        row = await session.get(Strategy, strategy_id)
        event = await session.scalar(select(Event).where(Event.message.contains(RISK_RECOVERY_REASON)))
        recovery = await session.get(RuntimeState, "risk-recovery:upbit_usdt:live")
        assert not account.halted and account.high_water == account.daily_anchor == D(1000)
        assert row.status == "RUNNING" and row.reason == RISK_RECOVERY_REASON
        assert row.state["last_mid"] is None and row.state["last_bar"] is None
        assert event.data["anchors_reset"] is False
        assert recovery.data["status"] == "RECOVERED"
    engine.upbit.test_order.assert_awaited()
    engine.upbit.place.assert_not_awaited()
    engine.upbit.cancel.assert_not_awaited()


@pytest.mark.parametrize("obstruction", ["disabled", "unsafe", "unstable", "pending", "manual"])
async def test_auto_recovery_stays_halted_until_every_gate_passes(db, obstruction):
    engine = await fixture(db)
    engine.settings.upbit_usdt_auto_recover = obstruction != "disabled"
    engine.settings.daily_loss_usdt = D(80)
    engine.settings.drawdown_usdt = D(400)
    strategy_id = await halted_strategy(
        engine, reason="사용자 중단 — 보유 유지" if obstruction == "manual" else LEGACY_RISK_HALT_REASON
    )
    async with engine.sessions.begin() as session:
        if obstruction == "unsafe":
            (await session.get(RuntimeState, "portfolio:upbit_usdt:live")).data = valuation("930")
        elif obstruction == "unstable":
            stable = await session.scalar(select(Snapshot))
            stable.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=10)
        elif obstruction == "pending":
            session.add(
                Intent(
                    strategy_id=strategy_id,
                    venue="upbit_usdt",
                    mode="live",
                    symbol="USDT-SOL",
                    side="SELL",
                    quantity=D(1),
                    price=D(110),
                    status="PENDING",
                )
            )

    assert not await engine.auto_recover_upbit_usdt(datetime.now(UTC))

    async with engine.sessions() as session:
        account = await session.get(AccountState, {"venue": "upbit_usdt", "mode": "live"})
        row = await session.get(Strategy, strategy_id)
        assert account.halted and row.status == "PAUSED"
    engine.upbit.place.assert_not_awaited()


async def test_failed_live_preflight_blocks_auto_recovery_without_partial_state_change(db):
    engine = await fixture(db)
    engine.settings.upbit_usdt_auto_recover = True
    engine.settings.daily_loss_usdt = D(80)
    engine.settings.drawdown_usdt = D(400)
    strategy_id = await halted_strategy(engine)
    engine.upbit.chance.return_value["ask_account"]["balance"] = "1"

    assert not await engine.auto_recover_upbit_usdt(datetime.now(UTC))

    async with engine.sessions() as session:
        account = await session.get(AccountState, {"venue": "upbit_usdt", "mode": "live"})
        row = await session.get(Strategy, strategy_id)
        recovery = await session.get(RuntimeState, "risk-recovery:upbit_usdt:live")
        assert account.halted and row.status == "PAUSED"
        assert recovery.data["status"] == "BLOCKED"
    engine.upbit.place.assert_not_awaited()


async def test_btc_and_sol_recover_together_after_every_preflight_passes(db):
    engine, sol_id, btc_id, _ = await setup_pair(db)
    engine.settings.upbit_usdt_auto_recover = True
    engine.settings.daily_loss_usdt = D(80)
    engine.settings.drawdown_usdt = D(400)
    btc_chance = engine.upbit.chance.return_value
    sol_chance = usdt_chance()
    btc_quote = Quote("USDT-BTC", D(50000), D("50000.01"), datetime.now(UTC), D(1), D(1))
    sol_quote = usdt_quote()
    engine.upbit.chance.side_effect = lambda symbol: btc_chance if symbol == "USDT-BTC" else sol_chance
    engine.upbit.orderbooks.side_effect = lambda symbols: [
        btc_quote if symbols[0] == "USDT-BTC" else sol_quote
    ]
    engine.upbit.orders.return_value = []
    async with engine.sessions.begin() as session:
        sol, btc = await session.get(Strategy, sol_id), await session.get(Strategy, btc_id)
        btc.state = {**funded_state(StrategySpec.model_validate(btc.config)), "funded": True}
        for row in (sol, btc):
            row.status, row.reason = "PAUSED", LEGACY_RISK_HALT_REASON
        for intent in (await session.scalars(select(Intent))).all():
            intent.status = "CANCELED"
        account = await session.get(AccountState, {"venue": "upbit_usdt", "mode": "live"})
        account.high_water = account.daily_anchor = D(1000)
        account.halted, account.reason = True, LEGACY_RISK_HALT_REASON
        session.add(RuntimeState(key="portfolio:upbit_usdt:live", data=valuation()))
        session.add(
            Snapshot(
                venue="upbit_usdt",
                mode="live",
                data=valuation(),
                created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=61),
            )
        )

    assert await engine.auto_recover_upbit_usdt(datetime.now(UTC))

    async with engine.sessions() as session:
        rows = [await session.get(Strategy, strategy_id) for strategy_id in (btc_id, sol_id)]
        assert all(row.status == "RUNNING" and row.reason == RISK_RECOVERY_REASON for row in rows)
    engine.upbit.place.assert_not_awaited()
