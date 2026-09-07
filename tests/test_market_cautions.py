"""Caution exceptions are explicit, per strategy, and checked before orders."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from test_parallel_upbit_start import books, setup_pair
from test_usdt_maker import fixture, request, running, spec

from cotrader.domain import D, StrategySpec
from cotrader.markets import UPBIT_CAUTION_LABELS
from cotrader.models import Event, Intent, Strategy, now
from cotrader.services import approval_digest, create_strategy, enqueue, process_command
from cotrader.upbit import market_eligible

VOLUME = "TRADING_VOLUME_SOARING"


def market(**cautions):
    return {
        "market_event": {
            "warning": False,
            "caution": {**dict.fromkeys(UPBIT_CAUTION_LABELS, False), **cautions},
        }
    }


def test_caution_policy_defaults_are_strict_and_allow_only_known_upbit_categories():
    assert spec().allowed_market_cautions == ()
    assert not market_eligible(market(**{VOLUME: True}))
    assert market_eligible(market(**{VOLUME: True}), [VOLUME])
    assert not market_eligible(market(**{VOLUME: True, "PRICE_FLUCTUATIONS": True}), [VOLUME])
    assert market_eligible(
        market(**{VOLUME: True, "PRICE_FLUCTUATIONS": True}), [VOLUME, "PRICE_FLUCTUATIONS"]
    )
    assert not market_eligible(market(), ["FUTURE_CAUTION"])
    assert not market_eligible(market(FUTURE_CAUTION=True), list(UPBIT_CAUTION_LABELS))
    for allowed in (["warning"], ["FUTURE_CAUTION"], True, None):
        with pytest.raises(ValueError):
            StrategySpec.model_validate({**spec().model_dump(), "allowed_market_cautions": allowed})
    with pytest.raises(ValueError, match="업비트"):
        StrategySpec(symbol="TEST", budget="1000", kind="trend", allowed_market_cautions=[VOLUME])


@pytest.mark.parametrize(
    "event",
    [
        None,
        {},
        {"warning": True, "caution": {VOLUME: True}},
        {"caution": {VOLUME: True}},
        {"warning": "false", "caution": {VOLUME: True}},
        {"warning": False, "caution": None},
        {"warning": False, "caution": {}},
        {"warning": False, "caution": {VOLUME: "true"}},
        {"warning": False, "caution": {VOLUME: 1}},
        {"warning": False, "caution": {VOLUME: None}},
    ],
)
def test_no_caution_toggle_can_allow_warning_or_unconfirmed_market_state(event):
    assert not market_eligible({"market_event": event}, list(UPBIT_CAUTION_LABELS))


async def change_policy(session, row, settings, allowed, command_id="fixture-policy"):
    command = await enqueue(
        session,
        command_id,
        "set_market_cautions",
        {
            "strategy_id": row.id,
            "version": row.version,
            "approval": approval_digest(row, settings),
            "allowed_market_cautions": allowed,
        },
        "fixture",
    )
    await process_command(session, command, settings)
    return command


async def test_policy_save_preserves_sol_and_btc_books_and_invalidates_old_start_approval(db):
    engine, source_id, target_id, _ = await setup_pair(db)
    engine.upbit.markets.return_value["USDT-BTC"] = market(**{VOLUME: True})
    async with engine.sessions.begin() as session:
        target = await session.get(Strategy, target_id)
        before = await books(session)
        config = dict(target.config)
        old_approval, old_version = approval_digest(target, engine.settings), target.version
        with pytest.raises(ValueError, match="유의·주의"):
            await engine.check_live_start(target, session)
        command = await change_policy(session, target, engine.settings, [VOLUME])
        assert command.status == "SUCCEEDED"
        assert await books(session) == before
        assert target.config == {**config, "allowed_market_cautions": [VOLUME]}
        assert target.version == old_version + 1
        assert approval_digest(target, engine.settings) != old_approval
        assert (await session.get(Strategy, source_id)).config["allowed_market_cautions"] == []
        await engine.check_live_start(target, session)
        event = await session.scalar(select(Event).where(Event.kind == "audit"))
        assert event.data == {
            "actor": "fixture",
            "version": target.version,
            "previous": [],
            "allowed_market_cautions": [VOLUME],
        }
        start = await enqueue(
            session,
            "stale-start",
            "start",
            {
                "strategy_id": target.id,
                "version": old_version,
                "approval": old_approval,
            },
            "fixture",
        )
        with pytest.raises(ValueError, match="버전"):
            await process_command(session, start, engine.settings, live_checked=True)
        start.payload = {**start.payload, "version": target.version}
        with pytest.raises(ValueError, match="승인한"):
            await process_command(session, start, engine.settings, live_checked=True)
        # Turning the exception off restores blocking without altering any holdings.
        await change_policy(session, target, engine.settings, [], "fixture-disable")
        with pytest.raises(ValueError, match="유의·주의"):
            await engine.check_live_start(target, session)
        assert await books(session) == before
    engine.upbit.place.assert_not_awaited()
    engine.upbit.cancel.assert_not_awaited()


@pytest.mark.parametrize(
    "obstruction",
    ["running", "archived", "pending", "unknown", "version", "approval", "expired", "unsupported", "extra"],
)
async def test_policy_change_rejects_unsafe_or_stale_requests_without_partial_update(db, obstruction):
    engine = await fixture(db)
    async with engine.sessions.begin() as session:
        row = await create_strategy(session, "fixture", spec(), "live")
        if obstruction in {"running", "archived"}:
            row.status = obstruction.upper()
        if obstruction in {"pending", "unknown"}:
            session.add(
                Intent(
                    strategy_id=row.id,
                    venue=row.venue,
                    mode=row.mode,
                    symbol=row.symbol,
                    side="SELL",
                    quantity=D(1),
                    price=D(107),
                    status=obstruction.upper(),
                )
            )
        command = await enqueue(
            session,
            "fixture-policy",
            "set_market_cautions",
            {
                "strategy_id": row.id,
                "version": row.version,
                "approval": approval_digest(row, engine.settings),
                "allowed_market_cautions": [VOLUME],
            },
            "fixture",
        )
        if obstruction == "version":
            command.payload = {**command.payload, "version": row.version + 1}
        elif obstruction == "approval":
            command.payload = {**command.payload, "approval": "0" * 16}
        elif obstruction == "expired":
            command.created_at = now() - timedelta(minutes=6)
        elif obstruction == "unsupported":
            command.payload = {**command.payload, "allowed_market_cautions": ["warning"]}
        elif obstruction == "extra":
            command.payload = {**command.payload, "budget": "9999"}
        before = json.dumps([row.config, row.state, row.version, row.status], sort_keys=True)
        with pytest.raises(ValueError):
            await process_command(session, command, engine.settings)
        assert json.dumps([row.config, row.state, row.version, row.status], sort_keys=True) == before
        assert await session.scalar(select(func.count()).select_from(Event)) == 0


@pytest.mark.parametrize("allowed", [False, True])
async def test_inventory_preparation_checks_and_persists_the_selected_policy(db, allowed):
    engine = await fixture(db)
    engine.upbit.markets.return_value["USDT-SOL"] = market(**{VOLUME: True})
    async with engine.sessions.begin() as session:
        payload = request(allowed_market_cautions=[VOLUME] if allowed else []).model_dump(mode="json")
        if allowed:
            row = await engine.prepare_inventory(session, payload)
            assert row.config["allowed_market_cautions"] == [VOLUME]
            assert row.status == "DRAFT" and not row.state["funded"]
        else:
            with pytest.raises(ValueError, match="유의·주의"):
                await engine.prepare_inventory(session, payload)
        assert await session.scalar(select(func.count()).select_from(Intent)) == 0
    engine.upbit.place.assert_not_awaited()


@pytest.mark.parametrize(
    "allowed, warning, extra, expected",
    [
        (False, False, False, False),
        (True, False, False, True),
        (True, True, False, False),
        (True, False, True, False),
    ],
)
async def test_evaluation_and_final_dispatch_share_policy_and_keep_other_guards(
    db, allowed, warning, extra, expected
):
    engine = await fixture(db)
    owner_id = await running(engine)
    event = market(**{VOLUME: True, "PRICE_FLUCTUATIONS": extra})
    event["market_event"]["warning"] = warning
    engine.upbit_markets["USDT-SOL"] = event
    async with engine.sessions.begin() as session:
        row = await session.get(Strategy, owner_id)
        row.config = {**row.config, "allowed_market_cautions": [VOLUME] if allowed else []}
    await engine.evaluate(datetime.now(UTC))
    async with engine.sessions.begin() as session:
        intents = (await session.scalars(select(Intent))).all()
        assert len(intents) == (1 if expected else 0)
        if expected:
            intent_id = intents[0].id
        else:
            # A signal prepared earlier must still be rejected at the final check.
            intent = Intent(
                strategy_id=owner_id,
                venue="upbit_usdt",
                mode="live",
                symbol="USDT-SOL",
                side="SELL",
                price=D("106.17"),
                quantity=D("1.1"),
                slot=0,
            )
            session.add(intent)
            await session.flush()
            intent_id = intent.id
    engine.upbit.place.return_value = {"orderId": "fixture-order"}
    await engine.submit_live(intent_id)
    assert engine.upbit.place.await_count == int(expected)
    async with engine.sessions() as session:
        assert (await session.get(Intent, intent_id)).status == ("PENDING" if expected else "REJECTED")
