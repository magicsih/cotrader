import base64
import hashlib
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from urllib.parse import unquote, urlencode

import httpx
import pytest
from sqlalchemy import func, select

from cotrader.broker import BrokerError
from cotrader.config import Settings
from cotrader.domain import D, Quote, StrategySpec
from cotrader.engine import Engine
from cotrader.live import upbit_policy
from cotrader.models import Intent, Ledger, Strategy
from cotrader.services import approval_digest, bootstrap, create_strategy, enqueue, process_command
from cotrader.upbit import UpbitBroker, normalize_order


def settings(**overrides):
    return Settings(
        **{
            **dict(
                runtime_role="engine",
                auth_mode="github",
                upbit_enabled=True,
                upbit_live_enabled=True,
                UPBIT_OPEN_API_ACCESS_KEY="fixture-access",
                UPBIT_OPEN_API_SECRET_KEY="fixture-secret",
            ),
            **overrides,
        }
    )


def chance(**changes):
    return {
        **dict(
            bid_fee="0.0005",
            ask_fee="0.0005",
            maker_bid_fee="0.0005",
            maker_ask_fee="0.0005",
            market=dict(
                id="KRW-BTC",
                state="active",
                order_sides=["bid", "ask"],
                bid_types=["limit"],
                ask_types=["limit"],
                bid={"min_total": "5000"},
                ask={"min_total": "5000"},
                max_total="1000000000",
            ),
            bid_account=dict(currency="KRW", balance="1000000", locked="0"),
            ask_account=dict(currency="BTC", balance="0", locked="0"),
        ),
        **changes,
    }


def raw_order(identifier="client-1", state="wait", volume="0.002", filled="0.001", fee="45"):
    return dict(
        uuid="broker-1",
        identifier=identifier,
        market="KRW-BTC",
        side="bid",
        price="90000000",
        volume=volume,
        executed_volume=filled,
        paid_fee=fee,
        state=state,
        trades=[dict(volume=filled, funds=str(D(filled) * 90000000))] if D(filled) else [],
    )


def intent():
    return Intent(
        id="client-1",
        venue="upbit",
        mode="live",
        symbol="KRW-BTC",
        side="BUY",
        quantity=D("0.002"),
        price=D("90000000"),
    )


async def test_signed_get_arrays_post_and_cancel_bind_exact_parameters():
    calls = []

    async def responder(request):
        calls.append(request)
        payload = json.loads(base64.urlsafe_b64decode(request.headers["Authorization"].split(".")[1] + "=="))
        if request.method == "POST":
            body = json.loads(request.content)
            query = unquote(urlencode(body, doseq=True))
            assert body["identifier"] == "client-1" and body["smp_type"] == "cancel_taker"
        else:
            query = unquote(request.url.query.decode())
        assert payload["query_hash"] == hashlib.sha512(query.encode()).hexdigest()
        if request.url.path == "/v1/orders/open":
            assert request.url.params.get_list("states[]") == ["wait", "watch"]
            return httpx.Response(200, json=[])
        return httpx.Response(
            201 if request.method == "POST" else 200, json={"uuid": "broker-1", "identifier": "client-1"}
        )

    async with httpx.AsyncClient(
        base_url="https://api.upbit.com", transport=httpx.MockTransport(responder)
    ) as client:
        broker = UpbitBroker(settings(), client)
        await broker.orders()
        assert (await broker.place(intent()))["orderId"] == "broker-1"
        await broker.cancel("broker-1")
    assert len(calls) == 3


async def test_upbit_dry_run_works_with_live_disabled_and_never_returns_order_id():
    calls = []

    async def responder(request):
        calls.append(request.url.path)
        assert request.url.path == "/v1/orders/test"
        return httpx.Response(201, json={"uuid": "not-a-real-order"})

    async with httpx.AsyncClient(
        base_url="https://api.upbit.com", transport=httpx.MockTransport(responder)
    ) as client:
        broker = UpbitBroker(settings(upbit_live_enabled=False), client)
        assert await broker.test_order(intent()) == {"validated": True, "actual_order_created": False}
        with pytest.raises(BrokerError, match="read-only"):
            await broker.place(intent())
        with pytest.raises(BrokerError, match="read-only"):
            await broker.cancel("broker-1")
    assert calls == ["/v1/orders/test"]


@pytest.mark.parametrize(
    "method,path", [("POST", "/v1/withdraws/coin"), ("DELETE", "/v1/orders"), ("PUT", "/v1/order")]
)
async def test_even_live_transport_cannot_reach_unapproved_endpoints(method, path):
    broker = UpbitBroker(settings())
    try:
        with pytest.raises(BrokerError, match="endpoint-disabled"):
            await broker.request(method, path, private=True)
    finally:
        await broker.close()


@pytest.mark.parametrize("failure", ["timeout", "missing_uuid", "server_error"])
async def test_uncertain_order_submission_is_never_classified_as_rejected(failure):
    async def responder(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("fixture")
        return httpx.Response(503 if failure == "server_error" else 201, json={})

    async with httpx.AsyncClient(
        base_url="https://api.upbit.com", transport=httpx.MockTransport(responder)
    ) as client:
        with pytest.raises(BrokerError) as exc:
            await UpbitBroker(settings(), client).place(intent())
        assert exc.value.ambiguous


def test_partial_cancel_uses_actual_funds_and_fee():
    result = normalize_order(raw_order(state="cancel"))
    assert result["status"] == "CANCELED"
    assert result["execution"] == {
        "filledQuantity": "0.001",
        "filledAmount": "90000.000",
        "commission": "45",
        "tax": "0",
    }


@pytest.mark.parametrize(
    "changes", [{"trades": []}, {"paid_fee": "-1"}, {"executed_volume": "NaN"}, {"state": "done"}]
)
def test_incomplete_or_invalid_execution_evidence_is_rejected(changes):
    with pytest.raises(BrokerError, match="evidence"):
        normalize_order({**raw_order(), **changes})


def test_policy_rejects_changed_fee_unsupported_side_and_minimum():
    with pytest.raises(ValueError, match="수수료"):
        upbit_policy(chance(ask_fee="0.002"), "KRW-BTC", D("0.001"))
    with pytest.raises(ValueError, match="최소"):
        upbit_policy(chance(), "KRW-BTC", D("0.001"), side="bid", total=D(4999))
    with pytest.raises(ValueError, match="지원"):
        upbit_policy(chance(market={**chance()["market"], "ask_types": ["market"]}), "KRW-BTC", D("0.001"))


async def setup_crypto_engine(db):
    database, sessions = db
    engine = Engine(settings())
    await engine.db.dispose()
    await engine.broker.close()
    await engine.upbit.close()
    engine.db, engine.sessions, engine.lock = database, sessions, AsyncMock()
    engine.broker = AsyncMock()
    engine.upbit = AsyncMock()
    engine.upbit.chance.return_value = chance()
    engine.upbit.orders.return_value = []
    engine.upbit.account_snapshot.return_value = {"assets": []}
    market = {"market_event": {"warning": False, "caution": {"PRICE_FLUCTUATIONS": False}}}
    engine.upbit_markets = {"KRW-BTC": market}
    engine.upbit.markets.return_value = engine.upbit_markets
    quote = Quote("KRW-BTC", D(90000000), D(90000000), datetime.now(UTC), D(1), D(1))
    engine.quotes[quote.symbol] = quote
    engine.upbit.orderbooks.return_value = [quote]
    async with sessions.begin() as session:
        await bootstrap(session, engine.settings)
        strategy = await create_strategy(
            session,
            "실거래 점검",
            StrategySpec(
                venue="upbit",
                symbol="KRW-BTC",
                budget="1000000",
                lower="80000000",
                upper="100000000",
                grids=4,
            ),
            "live",
        )
        return engine, strategy.id


async def test_live_start_cannot_bypass_current_account_checks_or_venue_flag(db):
    engine, strategy_id = await setup_crypto_engine(db)
    async with engine.sessions.begin() as session:
        strategy = await session.get(Strategy, strategy_id)
        cmd = await enqueue(
            session,
            "start",
            "start",
            dict(strategy_id=strategy_id, version=1, approval=approval_digest(strategy, engine.settings)),
            "fixture",
        )
        with pytest.raises(ValueError, match="최신 계좌"):
            await process_command(session, cmd, engine.settings)
        engine.settings.upbit_live_enabled = False
        engine.settings.live_enabled = True
        cmd.payload = {**cmd.payload, "approval": approval_digest(strategy, engine.settings)}
        with pytest.raises(ValueError, match="허용하지"):
            await process_command(session, cmd, engine.settings, live_checked=True)
        assert strategy.status == "DRAFT" and not strategy.state["funded"]


@pytest.mark.parametrize("obstruction", ["holdings", "manual_order", "cash"])
async def test_readiness_rejects_personal_holdings_manual_orders_and_insufficient_cash(db, obstruction):
    engine, strategy_id = await setup_crypto_engine(db)
    if obstruction == "holdings":
        engine.upbit.chance.return_value = chance(
            ask_account=dict(currency="BTC", balance="0.01", locked="0")
        )
    elif obstruction == "manual_order":
        engine.upbit.orders.return_value = [{"symbol": "KRW-BTC", "orderId": "manual"}]
    else:
        engine.upbit.chance.return_value = chance(bid_account=dict(currency="KRW", balance="1", locked="0"))
    async with engine.sessions() as session:
        with pytest.raises(ValueError):
            await engine.check_live_start(await session.get(Strategy, strategy_id))
    engine.upbit.place.assert_not_awaited()
    engine.upbit.cancel.assert_not_awaited()


async def test_readiness_performs_only_dry_run_and_leaves_strategy_unfunded(db):
    engine, strategy_id = await setup_crypto_engine(db)
    async with engine.sessions() as session:
        await engine.check_live_start(await session.get(Strategy, strategy_id))
        assert (await session.get(Strategy, strategy_id)).status == "DRAFT"
        assert await session.scalar(select(func.count()).select_from(Intent)) == 0
    engine.upbit.test_order.assert_awaited_once()
    engine.upbit.place.assert_not_awaited()


async def test_unknown_upbit_submission_is_looked_up_after_pause_without_resubmission(db):
    engine, strategy_id = await setup_crypto_engine(db)
    async with engine.sessions.begin() as session:
        row = await session.get(Strategy, strategy_id)
        row.status = "RUNNING"
        row.state = {**row.state, "funded": True}
        order = intent()
        order.strategy_id = strategy_id
        session.add(order)
    engine.upbit.place.side_effect = BrokerError("upbit-unavailable", ambiguous=True)
    await engine.submit_live("client-1")
    async with engine.sessions.begin() as session:
        assert (await session.get(Intent, "client-1")).status == "UNKNOWN"
        (await session.get(Strategy, strategy_id)).status = "PAUSED"
    engine.upbit.order.side_effect = BrokerError("upbit-order_not_found")
    await engine.reconcile_orders()
    engine.upbit.order.assert_awaited_once_with(identifier="client-1")
    assert engine.upbit.place.await_count == 1
    engine.upbit.order.side_effect = None
    engine.upbit.order.return_value = normalize_order(raw_order(state="cancel"))
    await engine.reconcile_orders()
    await engine.reconcile_orders()
    async with engine.sessions() as session:
        order = await session.get(Intent, "client-1")
        assert order.status == "CANCELED" and order.broker_id == "broker-1"
        assert D((await session.get(Strategy, strategy_id)).state["quantity"]) == D("0.001")
        assert await session.scalar(select(func.count()).select_from(Ledger)) == 1
    engine.broker.place.assert_not_awaited()


async def test_upbit_cancel_acknowledgement_is_not_terminal_and_late_fill_is_counted(db):
    engine, strategy_id = await setup_crypto_engine(db)
    async with engine.sessions.begin() as session:
        (await session.get(Strategy, strategy_id)).status = "PAUSED"
        order = intent()
        order.strategy_id = strategy_id
        order.status = "PARTIAL_FILLED"
        order.broker_id = "broker-1"
        session.add(order)
    await engine.cancel_stopped()
    engine.upbit.cancel.assert_awaited_once_with("broker-1")
    async with engine.sessions() as session:
        assert (await session.get(Intent, "client-1")).status == "PENDING_CANCEL"
    engine.upbit.order.return_value = normalize_order(raw_order(state="done", filled="0.002", fee="90"))
    await engine.reconcile_orders()
    async with engine.sessions() as session:
        assert (await session.get(Intent, "client-1")).status == "FILLED"
        assert D((await session.get(Strategy, strategy_id)).state["quantity"]) == D("0.002")
    engine.upbit.place.assert_not_awaited()


async def test_toss_uses_integer_wire_quantity_and_rejects_fractional_quantity():
    from types import SimpleNamespace

    from cotrader.broker import TossBroker

    broker = TossBroker(settings(upbit_live_enabled=False, live_enabled=True, market_source="toss"))
    broker.request = AsyncMock(return_value={"orderId": "toss-order"})
    order = SimpleNamespace(
        id="client-1", symbol="TEST", side="BUY", quantity=D("2.0000000000"), price=D("100")
    )
    try:
        await broker.place(order)
        assert broker.request.await_args.kwargs["body"]["quantity"] == "2"
        order.quantity = D("1.5")
        with pytest.raises(ValueError, match="정수"):
            await broker.place(order)
        assert broker.request.await_count == 1
    finally:
        await broker.close()
