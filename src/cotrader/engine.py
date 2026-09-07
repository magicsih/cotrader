import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from cotrader.broker import BrokerError, TossBroker, current_session
from cotrader.db import SingleWriter, database
from cotrader.domain import (
    ACTIVE_ORDERS,
    Bar,
    D,
    StrategySpec,
    aggregate,
    decide,
    funded_state,
    grid_quantity,
    levels,
    slot_quantity,
)
from cotrader.live import InventoryGridRequest, amount, inventory_grid_spec, upbit_policy
from cotrader.markets import (
    UPBIT_VENUES,
    VENUES,
    asset_key,
    currency,
    is_upbit,
    order_size_valid,
    portfolio_key,
    round_quantity,
    upbit_venue,
)
from cotrader.models import (
    AccountState,
    CandleRow,
    Command,
    Event,
    Intent,
    RuntimeState,
    Snapshot,
    Strategy,
    now,
)
from cotrader.services import (
    approval_digest,
    bootstrap,
    check_risk,
    create_strategy,
    process_command,
    put_runtime,
    record_execution,
    settlement_value,
)
from cotrader.upbit import UpbitBroker, market_eligible

LOG = logging.getLogger(__name__)


def stock_eligible(stock):
    return (
        stock.get("currency") == "USD"
        and stock.get("status") == "ACTIVE"
        and (
            stock.get("securityType") == "STOCK"
            and stock.get("isCommonShare") is True
            or stock.get("securityType") in {"ETF", "FOREIGN_ETF"}
            and D(str(stock.get("leverageFactor") or "0")) == 1
        )
    )


class Engine:
    def __init__(self, settings):
        self.settings = settings
        self.db, self.sessions = database(settings)
        self.broker = TossBroker(settings)
        self.upbit = UpbitBroker(settings)
        self.upbit_markets = {}
        self.last_upbit_quote = datetime.min.replace(tzinfo=UTC)
        self.last_upbit_account = self.last_upbit_quote
        self.last_upbit_markets = self.last_upbit_quote
        self.last_upbit_candle = None
        self.upbit_error = ""
        self.quotes = {}
        self.calendar = {}
        self.stocks = {}
        self.stream_task = None
        self.stream_symbols = None
        self.refresh = True
        self.last_market_refresh = datetime.min.replace(tzinfo=UTC)
        self.last_snapshot = self.last_market_refresh
        self.last_candle_minute = None
        self.reset_grid_reference = False
        self.actual_commission = None
        self.market_error = ""
        self.lock = None
        self.last_account_refresh = self.last_market_refresh

    async def check_live_start(self, strategy, session=None):
        """Fresh account readback; Upbit's test endpoint cannot create an order."""
        spec = StrategySpec.model_validate(strategy.config)
        if session is None:
            async with self.sessions() as read_session:
                return await self.check_live_start(strategy, read_session)
        pending = await session.scalar(
            select(Intent.id).where(
                Intent.venue.in_(UPBIT_VENUES if is_upbit(strategy.venue) else [strategy.venue]),
                Intent.mode == "live",
                Intent.status.in_(ACTIVE_ORDERS),
            )
        )
        intents = (
            await session.scalars(
                select(Intent).where(
                    Intent.venue.in_(UPBIT_VENUES if is_upbit(strategy.venue) else [strategy.venue]),
                    Intent.mode == "live",
                )
            )
        ).all()
        if pending:
            raise ValueError("기존 실거래 미체결·미확인 주문의 대조가 끝난 후 시작하세요")
        owned = {r.broker_id for r in intents if r.broker_id}
        if strategy.venue == "toss":
            snapshot = await self.broker.account_snapshot()
            cash = amount(snapshot["cash_buying_power"]["USD"])
            quantity = sum(
                (amount(r["quantity"]) for r in snapshot["holdings"]["items"] if r["symbol"] == spec.symbol),
                D(0),
            )
            if amount(snapshot["us_commission_rate"]) > spec.commission_rate:
                raise ValueError("실제 수수료가 전략의 비용 가정보다 높습니다")
            stocks = await self.broker.stocks([spec.symbol])
            stock = next((r for r in stocks if r["symbol"] == spec.symbol), {})
            if not stock_eligible(stock):
                raise ValueError("토스에서 지원하는 미국 일반 주식·비레버리지 ETF인지 확인하세요")
            orders = await self.broker.orders()
        else:
            if not self.settings.upbit_enabled:
                raise ValueError("업비트 연결이 필요합니다")
            markets = await self.upbit.markets()
            if not market_eligible(markets.get(spec.symbol)):
                raise ValueError("업비트 거래 유의·주의 상태 확인 필요")
            chance = await self.upbit.chance(spec.symbol)
            cash, sellable, quantity = upbit_policy(
                chance, spec.symbol, spec.commission_rate, maker_only=spec.execution_policy == "maker_only"
            )
            orders = await self.upbit.orders()
        if any(
            asset_key(strategy.venue, o["symbol"]) == asset_key(strategy.venue, spec.symbol)
            and o["orderId"] not in owned
            for o in orders
        ):
            raise ValueError("같은 종목의 수동 주문이 있습니다. 직접 정리한 후 다시 점검하세요")
        importing = bool(spec.inventory_quantity and not strategy.state.get("funded"))
        expected = spec.inventory_quantity if importing else D(strategy.state["quantity"])
        if quantity != expected:
            raise ValueError(
                "승인할 전량과 현재 계좌 수량이 다릅니다. 최신 보유 코인 초안을 다시 준비하세요"
                if importing
                else "기존 보유분 또는 봇 장부와 다른 수량이 있습니다. 자동으로 매도·편입하지 않습니다"
            )
        if cash < D(strategy.state["cash"]):
            raise ValueError("계좌의 사용 가능한 현금이 전략의 남은 현금 예산보다 적습니다")
        if is_upbit(strategy.venue):
            quotes = await self.upbit.orderbooks([spec.symbol])
            if not quotes or not quotes[0].valid(spec, datetime.now(UTC)):
                raise ValueError("신선한 업비트 호가·허용 호가 차이 확인 필요")
            if importing:
                if sellable != expected:
                    raise ValueError("편입할 코인의 일부가 잠겨 있습니다. 주문을 확인하세요")
                if abs(quotes[0].bid / spec.inventory_reference_price - 1) > D("0.005"):
                    raise ValueError(
                        "평가 기준가에서 0.5% 넘게 변했습니다. 최신 보유 코인 초안을 다시 준비하세요"
                    )
            if spec.inventory_quantity:
                from cotrader.models import uid

                state = funded_state(spec) if importing else strategy.state
                prices = levels(spec)
                for i in range(spec.grids):
                    lot = state["lots"][str(i)]
                    held = D(lot["quantity"])
                    buy_size = min(
                        grid_quantity(spec, i) - held,
                        round_quantity(D(lot["cash"]) / (prices[i] * (1 + spec.commission_rate)), spec.venue),
                    )
                    for side, size, price in (("SELL", held, prices[i + 1]), ("BUY", buy_size, prices[i])):
                        if order_size_valid(size, price, spec.venue):
                            upbit_policy(
                                chance,
                                spec.symbol,
                                spec.commission_rate,
                                side="ask" if side == "SELL" else "bid",
                                total=price * size,
                                maker_only=spec.execution_policy == "maker_only",
                            )
                            await self.upbit.test_order(
                                Intent(
                                    id=uid(),
                                    venue=spec.venue,
                                    symbol=spec.symbol,
                                    side=side,
                                    price=price,
                                    quantity=size,
                                ),
                                maker_only=spec.execution_policy == "maker_only",
                            )
                return
            price = levels(spec)[0] if spec.kind == "grid" and not quantity else quotes[0].ask
            size = round_quantity(quantity, spec.venue) if quantity else slot_quantity(spec, price)
            side = "SELL" if quantity else "BUY"
            upbit_policy(
                chance,
                spec.symbol,
                spec.commission_rate,
                side="ask" if quantity else "bid",
                total=price * size,
                maker_only=spec.execution_policy == "maker_only",
            )
            from cotrader.models import uid

            await self.upbit.test_order(
                Intent(id=uid(), venue=spec.venue, symbol=spec.symbol, side=side, price=price, quantity=size)
            )

    async def prepare_inventory(self, session, payload):
        request = InventoryGridRequest.model_validate(payload)
        venue = upbit_venue(request.symbol)
        if not self.settings.upbit_enabled:
            raise ValueError("업비트 연결이 필요합니다")
        existing = (
            await session.scalars(
                select(Strategy).where(Strategy.venue.in_(UPBIT_VENUES), Strategy.mode == "live")
            )
        ).all()
        funded = [
            row
            for row in existing
            if row.state.get("funded")
            and asset_key(row.venue, row.symbol) == asset_key(venue, request.symbol)
        ]
        source = None
        if request.source_strategy_id:
            source = next((row for row in funded if row.id == request.source_strategy_id), None)
            if not source or source.status == "ARCHIVED":
                raise ValueError("이관할 기존 전략·보유 코인을 확인하세요")
            if request.source_version != source.version or request.source_approval != approval_digest(
                source, self.settings
            ):
                raise ValueError("기존 전략 설정이 변경되었습니다. 이관 내용을 다시 확인하세요")
            if source.status == "RUNNING":
                raise ValueError("기존 전략을 중단하고 미체결 대조가 끝난 후 보유를 이관하세요")
        elif request.source_version is not None or request.source_approval is not None:
            raise ValueError("이관할 기존 전략 ID가 필요합니다")
        target = None
        if request.target_strategy_id:
            target = next((row for row in existing if row.id == request.target_strategy_id), None)
            if (
                not source
                or not target
                or target is source
                or target.venue != venue
                or target.symbol != request.symbol
                or target.status != "DRAFT"
                or target.state.get("funded")
                or target.state.get("settled")
                or target.state.get("transferred_from")
                or D(target.state["quantity"]) != 0
                or D(target.state["cash"]) != 0
            ):
                raise ValueError("인계받을 전략은 보유·현금 배정 전의 같은 종목 실거래 초안이어야 합니다")
            if request.target_version != target.version or request.target_approval != approval_digest(
                target, self.settings
            ):
                raise ValueError("새 초안 설정이 변경되었습니다. 인계 내용을 다시 확인하세요")
            if await session.scalar(select(Intent.id).where(Intent.strategy_id == target.id)):
                raise ValueError("주문 기록이 있는 전략에는 보유를 인계할 수 없습니다")
        elif request.target_version is not None or request.target_approval is not None:
            raise ValueError("인계받을 초안 ID가 필요합니다")
        if any(row is not source for row in funded):
            raise ValueError("같은 코인을 이미 운용하는 전략이 있습니다")
        if await session.scalar(
            select(Intent.id).where(
                Intent.venue.in_(UPBIT_VENUES), Intent.mode == "live", Intent.status.in_(ACTIVE_ORDERS)
            )
        ):
            raise ValueError("미체결·미확인 주문 대조가 끝난 후 준비하세요")
        markets = await self.upbit.markets()
        if not market_eligible(markets.get(request.symbol)):
            raise ValueError("업비트 거래 유의·주의 상태 확인 필요")
        chance = await self.upbit.chance(request.symbol)
        quotes = await self.upbit.orderbooks([request.symbol])
        if not quotes:
            raise ValueError("최신 호가를 확인할 수 없습니다")
        if target:
            approved = StrategySpec.model_validate(target.config)
            _, sellable, quantity = upbit_policy(
                chance,
                request.symbol,
                approved.commission_rate,
                maker_only=approved.execution_policy == "maker_only",
            )
            if (
                not approved.inventory_quantity
                or sellable != quantity
                or quantity != approved.inventory_quantity
            ):
                raise ValueError("새 초안의 편입 수량과 현재 매도 가능 전량이 다릅니다")
            # Preserve the reviewed price table; refresh only the inventory valuation.
            spec = StrategySpec.model_validate(
                {
                    **target.config,
                    "budget": quantity * quotes[0].bid,
                    "inventory_reference_price": quotes[0].bid,
                }
            )
        else:
            spec = inventory_grid_spec(request, chance, quotes[0])
        draft = Strategy(
            venue=venue,
            mode="live",
            symbol=spec.symbol,
            config=spec.model_dump(mode="json"),
            state={"funded": False, "quantity": "0", "cash": "0"},
        )
        await self.check_live_start(draft, session)
        account = await session.get(AccountState, {"venue": venue, "mode": "live"})
        if account.halted:
            raise ValueError("새 시장의 위험 중단 상태를 확인한 후 이관하세요")
        released_value = D(0)
        if source:
            if D(source.state["quantity"]) != spec.inventory_quantity:
                raise ValueError("기존 장부와 실제 보유 수량이 다릅니다")
            source_quotes = quotes if source.venue == venue else await self.upbit.orderbooks([source.symbol])
            if not source_quotes or not source_quotes[0].valid(
                StrategySpec.model_validate(source.config), datetime.now(UTC)
            ):
                raise ValueError("기존 시장의 최신 평가 가격을 확인할 수 없습니다")
            released_value = spec.inventory_quantity * source_quotes[0].bid
        allocated = sum(
            (
                D(r.config["budget"])
                for r in existing
                if r is not source and r.venue == venue and r.state.get("funded")
            ),
            D(0),
        )
        settled = sum(
            (
                settlement_value(r) - D(r.config["budget"])
                for r in existing
                if r.venue == venue and r.state.get("settled")
            ),
            D(0),
        )
        if source and source.venue == venue:
            settled += D(source.state["cash"]) + released_value - D(source.config["budget"])
        if spec.budget + allocated > min(account.capital, account.capital + settled):
            raise ValueError("새 시장의 배정 가능한 예산보다 보유 평가액이 큽니다")
        # All broker checks precede book mutation; no sale, commission or FX is recorded.
        row = target or await create_strategy(session, f"{spec.symbol} 보유 전량 반복 그리드", spec, "live")
        if target:
            target.config = spec.model_dump(mode="json")
            target.version += 1
        row.reason = "보유 장부 인계 완료 — 설정 확인·시작 대기" if source else row.reason
        if source:
            source_state = dict(source.state)
            source.state = {
                **source.state,
                "funded": False,
                "settled": True,
                "released_quantity": source.state["quantity"],
                "released_value": str(released_value),
                "released_currency": currency(source.venue),
                "closed_unrealized": str(released_value - D(source.state["cost_basis"])),
                "quantity": "0",
                "cost_basis": "0",
                "lots": {},
                "transferred_to": row.id,
            }
            source.status, source.reason = "ARCHIVED", "보유 장부 이관 — 실제 매도·환전 없음, 기존 현금 유지"
            row.state = {**row.state, "transferred_from": source.id}
            session.add(
                Event(
                    kind="transfer",
                    strategy_id=source.id,
                    message=source.reason,
                    data={
                        "target_id": row.id,
                        "quantity": str(spec.inventory_quantity),
                        "valuation": str(released_value),
                        "currency": currency(source.venue),
                        "source_state": source_state,
                        "actual_order_created": False,
                    },
                    notify=True,
                )
            )
        session.add(
            Event(
                kind="strategy",
                strategy_id=row.id,
                message="보유 전량 반복 그리드 초안 준비 — 시작 확인 대기",
            )
        )
        return row

    async def reconnect(self):
        self.refresh = True
        self.reset_grid_reference = True
        self.quotes = {k: v for k, v in self.quotes.items() if k.startswith(("KRW-", "USDT-"))}

    def on_quote(self, quote):
        previous = self.quotes.get(quote.symbol)
        if previous is None or quote.at > previous.at:
            self.quotes[quote.symbol] = quote

    def on_order(self, _order):
        # Keep websocket consumption nonblocking; REST is the reconciliation authority.
        self.refresh = True

    async def run(self):
        try:
            async with SingleWriter(self.db, "cotrader:engine") as lock:
                self.lock = lock
                async with self.sessions.begin() as session:
                    await bootstrap(session, self.settings)
                    for row in (await session.scalars(select(Strategy))).all():
                        row.state = {**row.state, "last_mid": None, "last_bar": None}
                while True:
                    await lock.verify()
                    try:
                        await self.cycle()
                    except BrokerError as exc:
                        async with self.sessions.begin() as session:
                            await put_runtime(
                                session,
                                "engine",
                                {
                                    "status": "WAITING",
                                    "reason": exc.code,
                                    "market_source": self.settings.market_source,
                                },
                            )
                        LOG.warning("broker unavailable: %s", exc.code)
                    await asyncio.sleep(1)
        finally:
            if self.stream_task:
                self.stream_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.stream_task
            await self.broker.close()
            await self.upbit.close()
            await self.db.dispose()

    async def cycle(self):
        async with self.sessions.begin() as session:
            commands = (
                await session.scalars(
                    select(Command)
                    .where(Command.status == "QUEUED")
                    .order_by(Command.created_at)
                    .limit(20)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for command in commands:
                try:
                    if command.action == "prepare_inventory":
                        if now() - command.created_at > timedelta(minutes=5):
                            raise ValueError("초안 준비 요청이 5분 이상 지연되었습니다. 다시 요청하세요")
                        row = await self.prepare_inventory(session, command.payload)
                        command.status, command.result = (
                            "SUCCEEDED",
                            {
                                "message": "보유 전량 반복 그리드 초안을 준비했습니다. 내 전략에서 가격과 수량을 확인하고 시작하세요.",
                                "strategy_id": row.id,
                                "actual_order_created": False,
                            },
                        )
                        continue
                    checked = False
                    if command.action in {"start", "check_live"}:
                        row = await session.get(Strategy, command.payload.get("strategy_id", ""))
                        if row and row.mode == "live":
                            await self.check_live_start(row, session)
                            checked = True
                    if command.action == "check_live":
                        if not checked:
                            raise ValueError("점검할 실거래 전략 초안이 없습니다")
                        command.status, command.result = (
                            "SUCCEEDED",
                            {
                                "message": "계좌·기존 주문·비용 점검 통과. 실제 주문은 생성하지 않았습니다.",
                                "ready": True,
                                "actual_order_created": False,
                            },
                        )
                    else:
                        await process_command(session, command, self.settings, live_checked=checked)
                except (ValueError, KeyError, TypeError, BrokerError, ArithmeticError) as exc:
                    command.status, command.result = "REJECTED", {"message": str(exc)}
                    session.add(Event(kind="command", message=f"명령 거절: {exc}", notify=True))
        async with self.sessions() as session:
            strategies = (
                await session.scalars(select(Strategy).where(Strategy.status.in_(["RUNNING", "PAUSED"])))
            ).all()
        symbols = sorted({s.symbol for s in strategies if s.venue == "toss" and s.state.get("funded")})
        crypto_symbols = sorted({s.symbol for s in strategies if is_upbit(s.venue) and s.state.get("funded")})
        at = datetime.now(UTC)
        if self.settings.account_reads_enabled and (at - self.last_account_refresh).total_seconds() >= 60:
            await self.refresh_account(at)
        if self.settings.market_source == "toss":
            try:
                await self.market_refresh(symbols, at)
                self.market_error = ""
            except BrokerError as exc:
                self.quotes = {k: v for k, v in self.quotes.items() if k.startswith(("KRW-", "USDT-"))}
                self.market_error = exc.code
        await self.resolve_commands()
        if self.settings.upbit_enabled:
            await self.upbit_refresh(crypto_symbols, at)
        market = current_session(self.calendar, at)
        # Every cycle reconciles before generating any new intent.
        execution_error = ""
        try:
            await self.reconcile_orders()
            await self.verify_holdings()
        except (BrokerError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
            execution_error = str(exc)
        async with self.sessions.begin() as session:
            if self.reset_grid_reference:
                for row in (await session.scalars(select(Strategy).where(Strategy.venue == "toss"))).all():
                    row.state = {**row.state, "last_mid": None}
                self.reset_grid_reference = False
            await check_risk(session, self.settings, self.quotes, market[1] if market else "")
            for venue in UPBIT_VENUES:
                await check_risk(session, self.settings, self.quotes, at.date().isoformat(), venue=venue)
        if not execution_error:
            await self.evaluate(at, toss_open=bool(market))
        await self.cancel_stopped()
        if not execution_error:
            await self.dispatch()
        if self.settings.market_source == "toss" or self.settings.upbit_enabled:
            await self.ingest_page()
        async with self.sessions.begin() as session:
            await put_runtime(
                session,
                "engine",
                {
                    "status": "RUNNING"
                    if market and not self.market_error and not execution_error
                    else "WAITING",
                    "reason": execution_error
                    or self.market_error
                    or (
                        "시세 연결 전"
                        if self.settings.market_source == "offline"
                        else (market[0] if market else "휴장 또는 세션 전환")
                    ),
                    "market_source": self.settings.market_source,
                    "live_enabled": self.settings.live_enabled,
                },
            )
            for symbol, quote in self.quotes.items():
                await put_runtime(
                    session,
                    f"quote:{symbol}",
                    {
                        "symbol": symbol,
                        "bid": str(quote.bid),
                        "ask": str(quote.ask),
                        "at": quote.at.isoformat(),
                    },
                )
            if (at - self.last_snapshot).total_seconds() >= 60:
                for venue in VENUES:
                    for mode in ("paper", "live"):
                        portfolio = await session.get(RuntimeState, portfolio_key(venue, mode))
                        if portfolio:
                            session.add(Snapshot(mode=mode, venue=venue, data=portfolio.data))
                self.last_snapshot = at

    async def refresh_account(self, at):
        self.last_account_refresh = at
        try:
            snapshot = await self.broker.account_snapshot()
            data = {"status": "CONNECTED", "snapshot": snapshot, "error": None}
        except (BrokerError, KeyError, TypeError, ValueError) as exc:
            async with self.sessions() as session:
                previous = await session.get(RuntimeState, "broker_account")
            data = {
                "status": "ERROR",
                "error": exc.code if isinstance(exc, BrokerError) else "invalid-account-response",
                "snapshot": previous.data.get("snapshot") if previous else None,
            }
        data["read_only"] = not self.settings.live_enabled
        async with self.sessions.begin() as session:
            await put_runtime(session, "broker_account", data)

    async def upbit_refresh(self, symbols, at):
        if self.settings.upbit_account_reads_enabled and (at - self.last_upbit_account).total_seconds() >= 60:
            self.last_upbit_account = at
            try:
                snapshot = await self.upbit.account_snapshot()
                data = {
                    "status": "CONNECTED",
                    "snapshot": snapshot,
                    "error": None,
                    "read_only": not (
                        self.settings.upbit_live_enabled or self.settings.upbit_usdt_live_enabled
                    ),
                }
            except (BrokerError, KeyError, TypeError, ValueError) as exc:
                async with self.sessions() as session:
                    previous = await session.get(RuntimeState, "upbit_account")
                data = {
                    "status": "ERROR",
                    "error": exc.code if isinstance(exc, BrokerError) else "invalid-upbit-response",
                    "snapshot": previous.data.get("snapshot") if previous else None,
                    "read_only": not (
                        self.settings.upbit_live_enabled or self.settings.upbit_usdt_live_enabled
                    ),
                }
            async with self.sessions.begin() as session:
                await put_runtime(session, "upbit_account", data)
        try:
            if not self.upbit_markets or (at - self.last_upbit_markets).total_seconds() >= 300:
                self.upbit_markets = await self.upbit.markets()
                self.last_upbit_markets = at
            valid = [s for s in symbols if s in self.upbit_markets]
            if (at - self.last_upbit_quote).total_seconds() >= 2:
                for quote in await self.upbit.orderbooks(valid):
                    self.on_quote(quote)
                self.last_upbit_quote = at
            minute = at.replace(second=0, microsecond=0)
            if self.last_upbit_candle != minute:
                for symbol in valid:
                    data = await self.upbit.candles(symbol)
                    async with self.sessions.begin() as session:
                        await self.save_candles(
                            session, symbol, "1m", data["candles"], venue=upbit_venue(symbol)
                        )
                self.last_upbit_candle = minute
            self.upbit_error = ""
        except (BrokerError, KeyError, TypeError, ValueError) as exc:
            self.upbit_error = exc.code if isinstance(exc, BrokerError) else "invalid-upbit-market-response"
            self.quotes = {k: v for k, v in self.quotes.items() if not k.startswith(("KRW-", "USDT-"))}
        async with self.sessions.begin() as session:
            await put_runtime(
                session,
                "engine:upbit",
                {
                    "status": "WAITING" if self.upbit_error else "RUNNING",
                    "reason": self.upbit_error or "24시간 시세 연결",
                    "market_source": "upbit",
                    "live_enabled": self.settings.upbit_live_enabled,
                },
            )

    async def fail_ingestion(self, exc, command_id):
        async with self.sessions.begin() as session:
            row = await session.scalar(
                select(Command).where(Command.id == command_id, Command.status == "RUNNING").with_for_update()
            )
            if row:
                row.status = "FAILED"
                row.result = {
                    **row.result,
                    "message": exc.code if isinstance(exc, BrokerError) else str(exc)[:300],
                }

    async def market_refresh(self, symbols, at):
        if self.refresh or (at - self.last_market_refresh).total_seconds() >= 60:
            self.calendar = await self.broker.calendar()
            if self.settings.account_seq is not None:
                self.actual_commission = await self.broker.commission_rate()
            self.stocks = {}
            for symbol in symbols:
                try:
                    rows = await self.broker.stocks([symbol])
                    stock = next((r for r in rows if r["symbol"] == symbol), None)
                    if not stock:
                        self.quotes.pop(symbol, None)
                        continue
                    self.stocks[symbol] = stock
                    quote = await self.broker.orderbook(symbol)
                    if quote:
                        self.on_quote(quote)
                except BrokerError:
                    self.quotes.pop(symbol, None)
            self.last_market_refresh, self.refresh = at, False
        valid_symbols = sorted(set(symbols) & self.stocks.keys())
        if self.stream_symbols != valid_symbols:
            if self.stream_task:
                self.stream_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.stream_task
            self.stream_symbols = valid_symbols
            self.stream_task = asyncio.create_task(
                self.broker.stream(valid_symbols, self.on_quote, self.on_order, self.reconnect)
            )
        # Candle truth comes from REST, never from summing lossy websocket ticks.
        candle_minute = at.replace(second=0, microsecond=0)
        if self.last_candle_minute != candle_minute:
            for symbol in valid_symbols:
                try:
                    data = await self.broker.candles(symbol)
                except BrokerError:
                    continue
                async with self.sessions.begin() as session:
                    await self.save_candles(session, symbol, "1m", data["candles"])
            self.last_candle_minute = candle_minute

    async def save_candles(self, session, symbol, interval, rows, venue="toss"):
        count = 0
        for item in rows:
            if item.get("currency") != currency(venue):
                continue
            bar = Bar.parse(item)
            # Exclude the currently forming candle.
            duration = timedelta(minutes=1) if interval == "1m" else timedelta(days=1)
            if bar.at + duration > datetime.now(UTC):
                continue
            stamp = bar.at.replace(tzinfo=None)
            exists = await session.scalar(
                select(CandleRow).where(
                    CandleRow.symbol == symbol,
                    CandleRow.venue == venue,
                    CandleRow.interval == interval,
                    CandleRow.timestamp == stamp,
                )
            )
            if exists:
                exists.data = bar.json()
            else:
                session.add(
                    CandleRow(
                        symbol=symbol,
                        venue=venue,
                        interval=interval,
                        timestamp=stamp,
                        data=bar.json(),
                        source=venue,
                    )
                )
                count += 1
        return count

    async def reconcile_orders(self):
        async with self.sessions() as session:
            intents = (
                await session.scalars(
                    select(Intent).where(Intent.status.in_(ACTIVE_ORDERS)).order_by(Intent.created_at)
                )
            ).all()
        for intent in intents:
            if intent.mode == "paper":
                await self.paper_fill(intent.id)
            elif is_upbit(intent.venue) and (intent.broker_id or intent.status in {"SENDING", "UNKNOWN"}):
                # A timed-out Upbit POST is never resubmitted. Identifier lookup survives restarts.
                try:
                    order = (
                        await self.upbit.order(intent.broker_id)
                        if intent.broker_id
                        else await self.upbit.order(identifier=intent.id)
                    )
                    if order.get("clientOrderId") != intent.id:
                        raise ValueError("업비트 주문 식별자가 내부 주문과 다릅니다")
                    async with self.sessions.begin() as session:
                        row = await session.get(Intent, intent.id)
                        await record_execution(session, row, order)
                        row.broker_id = order["orderId"]
                except (BrokerError, ValueError, KeyError) as exc:
                    async with self.sessions.begin() as session:
                        row = await session.get(Intent, intent.id)
                        row.status, row.reason = "UNKNOWN", f"업비트 주문 조회 대기: {exc}"
            elif intent.venue == "toss" and intent.broker_id and self.settings.market_source == "toss":
                order = await self.broker.order(intent.broker_id)
                async with self.sessions.begin() as session:
                    row = await session.get(Intent, intent.id)
                    try:
                        await record_execution(session, row, order)
                    except ValueError as exc:
                        row.status, row.reason = "UNKNOWN", str(exc)
            elif intent.status in {"SENDING", "UNKNOWN"}:
                async with self.sessions.begin() as session:
                    row = await session.get(Intent, intent.id)
                    strategy = await session.get(Strategy, row.strategy_id)
                    account = await session.get(AccountState, {"mode": row.mode, "venue": row.venue})
                    recoverable = (
                        row.submitted_at is not None
                        and now() - row.submitted_at < timedelta(minutes=9)
                        and strategy.status == "RUNNING"
                        and not account.halted
                    )
                    if not recoverable:
                        row.status, row.reason = (
                            "UNKNOWN",
                            "주문 응답 미확인 — 중복 방지 유효시간 밖 재전송 금지, 수동 대조 필요",
                        )
                if (
                    recoverable
                    and intent.venue == "toss"
                    and self.settings.market_source == "toss"
                    and self.settings.live_enabled
                ):
                    await self.submit_live(intent.id, recovery=True)
        # Actual fees can arrive after terminal state. Poll bounded oldest records.
        if self.settings.market_source == "toss" and self.settings.account_seq is not None:
            async with self.sessions() as session:
                rows = (
                    await session.scalars(
                        select(Intent)
                        .where(
                            Intent.mode == "live",
                            Intent.venue == "toss",
                            Intent.broker_id.is_not(None),
                            ~Intent.status.in_(ACTIVE_ORDERS),
                            Intent.costs_final.is_(False),
                            Intent.updated_at < now() - timedelta(minutes=1),
                        )
                        .order_by(Intent.updated_at)
                        .limit(3)
                    )
                ).all()
            for detached in rows:
                order = await self.broker.order(detached.broker_id)
                async with self.sessions.begin() as session:
                    row = await session.get(Intent, detached.id)
                    await record_execution(session, row, order)
                    row.updated_at = now()

    async def paper_fill(self, intent_id):
        async with self.sessions.begin() as session:
            intent = await session.get(Intent, intent_id)
            strategy = await session.get(Strategy, intent.strategy_id)
            if intent.status == "PREPARED":
                return
            if strategy.status != "RUNNING" or (
                strategy.config.get("execution_policy") != "maker_only"
                and now() - intent.created_at > timedelta(seconds=60)
            ):
                intent.status = "CANCELED"
                return
            quote = self.quotes.get(intent.symbol)
            spec = StrategySpec.model_validate(strategy.config)
            if (
                not quote
                or not quote.valid(spec, datetime.now(UTC))
                or quote.at.replace(tzinfo=None) <= intent.updated_at
            ):
                return
            price = quote.ask if intent.side == "BUY" else quote.bid
            crosses = price <= intent.price if intent.side == "BUY" else price >= intent.price
            depth = quote.ask_size if intent.side == "BUY" else quote.bid_size
            quantity = min(
                intent.quantity - intent.filled_quantity,
                round_quantity(depth * D("0.1"), spec.venue),
            )
            if not crosses or quantity <= 0:
                return
            filled = intent.filled_quantity + quantity
            amount = intent.filled_amount + quantity * (
                intent.price if spec.execution_policy == "maker_only" else price
            )
            await record_execution(
                session,
                intent,
                {
                    "symbol": intent.symbol,
                    "side": intent.side,
                    "status": "FILLED" if filled == intent.quantity else "PARTIAL_FILLED",
                    "execution": {
                        "filledQuantity": str(filled),
                        "filledAmount": str(amount),
                        "commission": str(amount * spec.commission_rate),
                        "tax": "0",
                    },
                },
            )

    async def verify_holdings(self):
        async with self.sessions() as session:
            strategies = (
                await session.scalars(
                    select(Strategy).where(Strategy.mode == "live", Strategy.status == "RUNNING")
                )
            ).all()
            intents = (await session.scalars(select(Intent).where(Intent.mode == "live"))).all()
        if not strategies:
            return
        holdings, open_orders = {}, []
        if any(s.venue == "toss" for s in strategies):
            holdings.update(
                {
                    asset_key("toss", r["symbol"]): D(r["quantity"])
                    for r in (await self.broker.holdings())["items"]
                }
            )
            open_orders.extend(
                (asset_key("toss", o["symbol"]), o["orderId"]) for o in await self.broker.orders()
            )
        if any(is_upbit(s.venue) for s in strategies):
            snapshot = await self.upbit.account_snapshot()
            holdings.update(
                {
                    "upbit:" + r["currency"]: amount(r["balance"]) + amount(r["locked"])
                    for r in snapshot["assets"]
                }
            )
            open_orders.extend(
                (asset_key("upbit", o["symbol"]), o["orderId"]) for o in await self.upbit.orders()
            )
        own_ids = {r.broker_id for r in intents if r.broker_id}
        pending_symbols = {asset_key(r.venue, r.symbol) for r in intents if r.status in ACTIVE_ORDERS}
        for strategy in strategies:
            key = asset_key(strategy.venue, strategy.symbol)
            foreign_order = any(
                order_key == key and order_id not in own_ids for order_key, order_id in open_orders
            )
            mismatch = key not in pending_symbols and holdings.get(key, D(0)) != D(strategy.state["quantity"])
            if foreign_order or mismatch:
                async with self.sessions.begin() as session:
                    row = await session.get(Strategy, strategy.id)
                    row.status, row.reason = "PAUSED", "수동 주문·기존 보유 또는 수량 불일치 — 계좌 대조 필요"
                    session.add(
                        Event(kind="reconciliation", strategy_id=row.id, message=row.reason, notify=True)
                    )

    async def cancel_stopped(self):
        async with self.sessions() as session:
            pairs = (
                await session.execute(
                    select(Intent, Strategy)
                    .join(Strategy, Strategy.id == Intent.strategy_id)
                    .where(Intent.status.in_(ACTIVE_ORDERS))
                )
            ).all()
        for intent, strategy in pairs:
            expired = strategy.config.get(
                "execution_policy"
            ) != "maker_only" and now() - intent.created_at > timedelta(seconds=60)
            if strategy.status == "RUNNING" and not expired:
                continue
            if intent.status == "PREPARED" or intent.mode == "paper":
                async with self.sessions.begin() as session:
                    row = await session.get(Intent, intent.id)
                    row.status = "CANCELED"
            elif intent.broker_id and self.settings.live_for(intent.venue):
                await self.lock.verify()
                try:
                    await (self.upbit if is_upbit(intent.venue) else self.broker).cancel(intent.broker_id)
                except BrokerError as exc:
                    if exc.code not in {"already-filled", "already-canceled", "already-processing"}:
                        LOG.warning("cancel pending: %s", exc.code)
                async with self.sessions.begin() as session:
                    row = await session.get(Intent, intent.id)
                    row.status, row.reason = "PENDING_CANCEL", "취소 요청 결과 확인 중"

    async def evaluate(self, at, toss_open=True):
        async with self.sessions.begin() as session:
            strategies = (await session.scalars(select(Strategy).where(Strategy.status == "RUNNING"))).all()
            for strategy in strategies:
                spec = StrategySpec.model_validate(strategy.config)
                if strategy.venue == "toss" and not toss_open:
                    strategy.reason = "휴장 또는 세션 전환"
                    continue
                if is_upbit(strategy.venue) and not self.settings.upbit_enabled:
                    strategy.reason = "업비트 시세 연결 대기"
                    continue
                if strategy.mode == "live" and not self.settings.live_for(strategy.venue):
                    strategy.reason = "서버 실거래 잠금"
                    continue
                if (
                    strategy.mode == "live"
                    and strategy.venue == "toss"
                    and (self.actual_commission is None or self.actual_commission > spec.commission_rate)
                ):
                    strategy.reason = "실제 계좌 수수료 확인 또는 비용 설정 재검토 필요"
                    continue
                portfolio = await session.get(RuntimeState, portfolio_key(strategy.venue, strategy.mode))
                if (
                    not portfolio
                    or not portfolio.data["complete"]
                    or portfolio.data["halted"]
                    or portfolio.data.get("unresolved_orders")
                ):
                    strategy.reason = "전체 평가금액 확인 또는 위험 중단 해제 필요"
                    continue
                stock = self.stocks.get(strategy.symbol, {})
                eligible = stock_eligible(stock)
                if is_upbit(strategy.venue):
                    market = self.upbit_markets.get(strategy.symbol)
                    eligible = market_eligible(market)
                if not eligible:
                    strategy.reason = "지원 종목·거래 상태 확인 필요"
                    continue
                quote = self.quotes.get(strategy.symbol)
                if not quote or not quote.valid(spec, at):
                    strategy.reason = "신선한 양방향 호가 또는 허용 호가 차이 조건 대기"
                    continue
                active = (
                    await session.scalars(
                        select(Intent).where(
                            Intent.mode == strategy.mode,
                            Intent.venue == strategy.venue,
                            Intent.symbol == strategy.symbol,
                            Intent.status.in_(ACTIVE_ORDERS),
                        )
                    )
                ).all()
                if active and (
                    spec.execution_policy != "maker_only"
                    or any(r.status in {"UNKNOWN", "SENDING", "PENDING_CANCEL"} for r in active)
                ):
                    strategy.reason = "기존 주문 결과 확인 중"
                    continue
                blocked_slots = {r.slot for r in active}
                if spec.execution_policy == "maker_only":
                    recent = (
                        await session.scalars(
                            select(Intent).where(
                                Intent.strategy_id == strategy.id,
                                Intent.status.in_(["REJECTED", "CANCELED"]),
                                Intent.filled_quantity == 0,
                                Intent.created_at > now() - timedelta(seconds=10),
                            )
                        )
                    ).all()
                    blocked_slots.update(r.slot for r in recent)
                rows = (
                    await session.scalars(
                        select(CandleRow)
                        .where(
                            CandleRow.symbol == strategy.symbol,
                            CandleRow.interval == "1m",
                            CandleRow.source == strategy.venue,
                            CandleRow.venue == strategy.venue,
                        )
                        .order_by(CandleRow.timestamp.desc())
                        .limit(spec.timeframe * (max(spec.slow, spec.rsi_period) + 10))
                    )
                ).all()
                bars = sorted([Bar.parse(row.data) for row in rows], key=lambda b: b.at)
                state = json.loads(json.dumps(strategy.state))
                decision, reason = decide(
                    spec, state, quote, aggregate(bars, spec.timeframe, at), blocked_slots=blocked_slots
                )
                strategy.state, strategy.reason = state, reason
                if reason == "GRID_LOWER_BREACH":
                    strategy.status, strategy.reason = "PAUSED", "그리드 하단 이탈 — 보유 유지"
                    session.add(
                        Event(
                            kind="risk",
                            strategy_id=strategy.id,
                            message=f"{strategy.symbol}: {strategy.reason}",
                            notify=True,
                        )
                    )
                elif decision:
                    if decision.side == "BUY" and decision.price * decision.quantity * (
                        1 + spec.commission_rate
                    ) > D(state["cash"]):
                        strategy.reason = "전략 현금 한도 부족"
                        continue
                    session.add(
                        Intent(
                            strategy_id=strategy.id,
                            venue=strategy.venue,
                            mode=strategy.mode,
                            symbol=strategy.symbol,
                            side=decision.side,
                            quantity=decision.quantity,
                            price=decision.price,
                            slot=decision.slot,
                            reason=decision.reason,
                        )
                    )
                    session.add(
                        Event(
                            kind="signal",
                            strategy_id=strategy.id,
                            message=f"{strategy.symbol}: {decision.reason}",
                            data={
                                "side": decision.side,
                                "price": str(decision.price),
                                "quantity": str(decision.quantity),
                            },
                        )
                    )

    async def dispatch(self):
        async with self.sessions() as session:
            intents = (await session.scalars(select(Intent).where(Intent.status == "PREPARED"))).all()
        for intent in intents:
            async with self.sessions() as session:
                account = await session.get(AccountState, {"mode": intent.mode, "venue": intent.venue})
                unknown = await session.scalar(
                    select(Intent.id).where(
                        Intent.mode == intent.mode,
                        Intent.venue.in_(UPBIT_VENUES if is_upbit(intent.venue) else [intent.venue]),
                        Intent.status.in_(["UNKNOWN", "SENDING"]),
                    )
                )
                portfolio = await session.get(RuntimeState, portfolio_key(intent.venue, intent.mode))
                if account.halted or unknown or not portfolio or not portfolio.data["complete"]:
                    continue
            if intent.mode == "paper":
                async with self.sessions.begin() as session:
                    row = await session.get(Intent, intent.id)
                    strategy = await session.get(Strategy, row.strategy_id)
                    row.status = "PENDING" if strategy.status == "RUNNING" else "CANCELED"
                    row.submitted_at = now()
            elif self.settings.live_for(intent.venue):
                await self.submit_live(intent.id)

    async def submit_live(self, intent_id, recovery=False):
        await self.lock.verify()
        async with self.sessions() as session:
            intent = await session.get(Intent, intent_id)
            strategy = await session.get(Strategy, intent.strategy_id)
            account = await session.get(AccountState, {"mode": "live", "venue": intent.venue})
        if intent.mode != "live" or intent.status not in (
            {"SENDING", "UNKNOWN"} if recovery else {"PREPARED"}
        ):
            return
        if strategy.status != "RUNNING" or account.halted or not self.settings.live_for(intent.venue):
            return
        maker_only = strategy.config.get("execution_policy") == "maker_only"
        if is_upbit(intent.venue) and recovery:
            return  # Identifier lookup only; never a second POST after an ambiguous response.
        if recovery and (intent.submitted_at is None or now() - intent.submitted_at >= timedelta(minutes=9)):
            return
        if not recovery:
            quote = self.quotes.get(intent.symbol)
            if (
                (intent.venue == "toss" and not current_session(self.calendar, datetime.now(UTC)))
                or not quote
                or not quote.valid(StrategySpec.model_validate(strategy.config), datetime.now(UTC))
            ):
                return
            if is_upbit(intent.venue):
                try:
                    chance = await self.upbit.chance(intent.symbol)
                    available, sellable, _ = upbit_policy(
                        chance,
                        intent.symbol,
                        D(strategy.config["commission_rate"]),
                        side="bid" if intent.side == "BUY" else "ask",
                        total=intent.price * intent.quantity,
                        maker_only=maker_only,
                    )
                    if maker_only:
                        crossed = (
                            intent.price <= quote.bid if intent.side == "SELL" else intent.price >= quote.ask
                        )
                        orders = await self.upbit.orders()
                        opposite = "BUY" if intent.side == "SELL" else "SELL"
                        crossed = crossed or any(
                            o["symbol"] == intent.symbol
                            and o.get("side") == opposite
                            and o.get("price") is not None
                            and (
                                intent.price <= D(o["price"])
                                if intent.side == "SELL"
                                else intent.price >= D(o["price"])
                            )
                            for o in orders
                        )
                        if crossed:
                            async with self.sessions.begin() as session:
                                row = await session.get(Intent, intent_id)
                                row.status, row.reason = (
                                    "CANCELED",
                                    "메이커 전용 가격·자전 체결 방지 조건 대기",
                                )
                            return
                    if not market_eligible(self.upbit_markets.get(intent.symbol)):
                        raise ValueError("업비트 거래 유의·주의 상태 확인 필요")
                    if intent.side == "BUY" and intent.price * intent.quantity * (
                        1 + D(strategy.config["commission_rate"])
                    ) > min(available, D(strategy.state["cash"])):
                        raise ValueError("업비트·전략 현금 한도 부족")
                    if intent.side == "SELL" and intent.quantity > min(
                        sellable, D(strategy.state["quantity"])
                    ):
                        raise ValueError("업비트·전략 매도 가능 수량 부족")
                except (ValueError, KeyError, ArithmeticError) as exc:
                    async with self.sessions.begin() as session:
                        row = await session.get(Intent, intent_id)
                        row.status, row.reason = "REJECTED", str(exc)
                        if "수수료" in str(exc):
                            owner = await session.get(Strategy, row.strategy_id)
                            owner.status, owner.reason = "PAUSED", str(exc)
                            session.add(
                                Event(kind="risk", strategy_id=owner.id, message=owner.reason, notify=True)
                            )
                    return
            elif intent.side == "BUY":
                available = await self.broker.buying_power()
                if intent.price * intent.quantity * (1 + D(strategy.config["commission_rate"])) > min(
                    available, D(strategy.state["cash"])
                ):
                    async with self.sessions.begin() as session:
                        row = await session.get(Intent, intent_id)
                        row.status, row.reason = "REJECTED", "증권사·전략 현금 한도 부족"
                    return
            elif intent.quantity > min(
                await self.broker.sellable(intent.symbol), D(strategy.state["quantity"])
            ):
                raise BrokerError("sellable-quantity-mismatch")
            quote = self.quotes.get(intent.symbol)
            if (
                (intent.venue == "toss" and not current_session(self.calendar, datetime.now(UTC)))
                or not quote
                or not quote.valid(StrategySpec.model_validate(strategy.config), datetime.now(UTC))
            ):
                return
        await self.lock.verify()
        async with self.sessions.begin() as session:
            intent = await session.get(Intent, intent_id)
            strategy = await session.get(Strategy, intent.strategy_id)
            if strategy.status != "RUNNING":
                return
            intent.status = "SENDING"
            if intent.submitted_at is None:
                intent.submitted_at = now()
        try:
            result = (
                await self.upbit.place(intent, maker_only=maker_only)
                if is_upbit(intent.venue)
                else await self.broker.place(intent)
            )
        except BrokerError as exc:
            async with self.sessions.begin() as session:
                row = await session.get(Intent, intent_id)
                row.status, row.reason = ("UNKNOWN" if exc.ambiguous else "REJECTED"), exc.code
                session.add(
                    Event(
                        kind="order",
                        strategy_id=row.strategy_id,
                        message=f"주문 응답: {exc.code}",
                        notify=True,
                    )
                )
            return
        async with self.sessions.begin() as session:
            row = await session.get(Intent, intent_id)
            row.broker_id, row.status = result["orderId"], "PENDING"

    async def ingest_page(self):
        async with self.sessions() as session:
            command = await session.scalar(
                select(Command)
                .where(Command.action == "ingest", Command.status == "RUNNING")
                .order_by(Command.created_at)
            )
        if not command:
            return
        try:
            progress = dict(command.result)
            payload = command.payload
            venue = payload.get("venue", "toss")
            if venue == "toss" and self.settings.market_source != "toss":
                raise ValueError("토스 시세 연결이 비활성입니다")
            if is_upbit(venue) and not self.settings.upbit_enabled:
                raise ValueError("업비트 시세 연결이 비활성입니다")
            broker = self.upbit if is_upbit(venue) else self.broker
            if (
                payload.get("require_eligible")
                and is_upbit(venue)
                and not market_eligible(self.upbit_markets.get(payload["symbol"]))
            ):
                raise ValueError("시작 후보의 현재 거래 유의·주의 상태를 확인할 수 없거나 해당 상태입니다")
            data = await broker.candles(
                payload["symbol"], payload.get("interval", "1m"), progress.get("before")
            )
            start = datetime.fromisoformat(payload["from"]).astimezone(UTC)
            end = datetime.fromisoformat(payload["to"]).astimezone(UTC) if payload.get("to") else None
            rows = [
                r
                for r in data["candles"]
                if Bar.parse(r).at >= start and (end is None or Bar.parse(r).at < end)
            ]
            async with self.sessions.begin() as session:
                row = await session.get(Command, command.id, with_for_update=True)
                if row.status != "RUNNING":
                    return
                count = await self.save_candles(
                    session, payload["symbol"], payload.get("interval", "1m"), rows, venue=venue
                )
                next_before = data.get("nextBefore")
                progress.update(
                    count=progress["count"] + count, pages=progress["pages"] + 1, before=next_before
                )
                if not next_before or any(Bar.parse(r).at < start for r in data["candles"]):
                    row.status = "SUCCEEDED"
                elif next_before == command.result.get("before") or progress["pages"] >= 1000:
                    row.status = "REJECTED"
                    progress["message"] = "페이지 진행 정지 또는 수집 상한 도달 — 수집된 범위를 확인하세요"
                row.result = progress
        except (BrokerError, ValueError, KeyError) as exc:
            await self.fail_ingestion(exc, command.id)

    async def resolve_commands(self):
        async with self.sessions() as session:
            commands = (
                await session.scalars(
                    select(Command).where(Command.action == "resolve", Command.status == "RUNNING")
                )
            ).all()
        for command in commands:
            try:
                async with self.sessions() as session:
                    detached = await session.get(Intent, command.payload["intent_id"])
                if not detached:
                    raise ValueError("대조할 주문이 없습니다")
                broker = self.upbit if is_upbit(detached.venue) else self.broker
                order = await broker.order(command.payload["broker_id"])
                async with self.sessions.begin() as session:
                    intent = await session.get(Intent, command.payload["intent_id"])
                    if not intent or intent.mode != "live" or intent.status != "UNKNOWN":
                        raise ValueError("대조할 미확인 실거래 주문이 없습니다")
                    if is_upbit(intent.venue) and order.get("clientOrderId") != intent.id:
                        raise ValueError("업비트 주문 식별자가 일치하지 않습니다")
                    if (
                        order["symbol"],
                        order["side"],
                        D(order["quantity"]),
                        D(order.get("price") or "0"),
                    ) != (intent.symbol, intent.side, intent.quantity, intent.price):
                        raise ValueError("종목·방향·수량·가격이 일치하지 않습니다")
                    used = await session.scalar(
                        select(Intent.id).where(Intent.broker_id == order["orderId"], Intent.id != intent.id)
                    )
                    if used:
                        raise ValueError("이미 다른 내부 주문에 연결된 증권사 주문입니다")
                    intent.broker_id = order["orderId"]
                    await record_execution(session, intent, order)
                    row = await session.get(Command, command.id)
                    row.status, row.result = (
                        "SUCCEEDED",
                        {"message": "사용자가 지정한 증권사 주문과 대조했습니다"},
                    )
            except (ValueError, KeyError, BrokerError) as exc:
                async with self.sessions.begin() as session:
                    row = await session.get(Command, command.id)
                    row.status, row.result = "REJECTED", {"message": str(exc)}
