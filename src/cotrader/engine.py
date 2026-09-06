import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from cotrader.broker import BrokerError, TossBroker, current_session
from cotrader.db import SingleWriter, database
from cotrader.domain import ACTIVE_ORDERS, Bar, D, StrategySpec, aggregate, decide
from cotrader.markets import VENUES, currency, portfolio_key, round_quantity
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
from cotrader.services import bootstrap, check_risk, process_command, put_runtime, record_execution
from cotrader.upbit import UpbitBroker, market_eligible

LOG = logging.getLogger(__name__)


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

    async def reconnect(self):
        self.refresh = True
        self.reset_grid_reference = True
        self.quotes = {k: v for k, v in self.quotes.items() if k.startswith("KRW-")}

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
                    await process_command(session, command, self.settings)
                except (ValueError, KeyError) as exc:
                    command.status, command.result = "REJECTED", {"message": str(exc)}
                    session.add(Event(kind="command", message=f"명령 거절: {exc}", notify=True))
        async with self.sessions() as session:
            strategies = (
                await session.scalars(select(Strategy).where(Strategy.status.in_(["RUNNING", "PAUSED"])))
            ).all()
        symbols = sorted({s.symbol for s in strategies if s.venue == "toss" and s.state.get("funded")})
        crypto_symbols = sorted(
            {s.symbol for s in strategies if s.venue == "upbit" and s.state.get("funded")}
        )
        at = datetime.now(UTC)
        if self.settings.account_reads_enabled and (at - self.last_account_refresh).total_seconds() >= 60:
            await self.refresh_account(at)
        if self.settings.market_source == "toss":
            try:
                await self.market_refresh(symbols, at)
                self.market_error = ""
            except BrokerError as exc:
                self.quotes = {k: v for k, v in self.quotes.items() if k.startswith("KRW-")}
                self.market_error = exc.code
            await self.resolve_commands()
        if self.settings.upbit_enabled:
            await self.upbit_refresh(crypto_symbols, at)
        market = current_session(self.calendar, at)
        # Every cycle reconciles before generating any new intent.
        await self.reconcile_orders()
        async with self.sessions.begin() as session:
            if self.reset_grid_reference:
                for row in (await session.scalars(select(Strategy).where(Strategy.venue == "toss"))).all():
                    row.state = {**row.state, "last_mid": None}
                self.reset_grid_reference = False
            await check_risk(session, self.settings, self.quotes, market[1] if market else "")
            await check_risk(session, self.settings, self.quotes, at.date().isoformat(), venue="upbit")
        if self.settings.market_source == "toss":
            await self.verify_holdings()
        await self.evaluate(at, toss_open=bool(market))
        await self.cancel_stopped()
        await self.dispatch()
        if self.settings.market_source == "toss" or self.settings.upbit_enabled:
            await self.ingest_page()
        async with self.sessions.begin() as session:
            await put_runtime(
                session,
                "engine",
                {
                    "status": "RUNNING" if market and not self.market_error else "WAITING",
                    "reason": self.market_error
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
                data = {"status": "CONNECTED", "snapshot": snapshot, "error": None, "read_only": True}
            except (BrokerError, KeyError, TypeError, ValueError) as exc:
                async with self.sessions() as session:
                    previous = await session.get(RuntimeState, "upbit_account")
                data = {
                    "status": "ERROR",
                    "error": exc.code if isinstance(exc, BrokerError) else "invalid-upbit-response",
                    "snapshot": previous.data.get("snapshot") if previous else None,
                    "read_only": True,
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
                        await self.save_candles(session, symbol, "1m", data["candles"], venue="upbit")
                self.last_upbit_candle = minute
            self.upbit_error = ""
        except (BrokerError, KeyError, TypeError, ValueError) as exc:
            self.upbit_error = exc.code if isinstance(exc, BrokerError) else "invalid-upbit-market-response"
            self.quotes = {k: v for k, v in self.quotes.items() if not k.startswith("KRW-")}
        async with self.sessions.begin() as session:
            await put_runtime(
                session,
                "engine:upbit",
                {
                    "status": "WAITING" if self.upbit_error else "RUNNING",
                    "reason": self.upbit_error or "24시간 시세 · 모의매매",
                    "market_source": "upbit",
                    "live_enabled": False,
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
            if strategy.status != "RUNNING" or now() - intent.created_at > timedelta(seconds=60):
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
            amount = intent.filled_amount + quantity * price
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
                    select(Strategy).where(
                        Strategy.mode == "live", Strategy.venue == "toss", Strategy.status == "RUNNING"
                    )
                )
            ).all()
            intents = (
                await session.scalars(select(Intent).where(Intent.mode == "live", Intent.venue == "toss"))
            ).all()
        if not strategies or self.settings.account_seq is None:
            return
        holdings = {r["symbol"]: D(r["quantity"]) for r in (await self.broker.holdings())["items"]}
        open_orders = await self.broker.orders()
        own_ids = {r.broker_id for r in intents if r.broker_id}
        pending_symbols = {r.symbol for r in intents if r.status in ACTIVE_ORDERS}
        for strategy in strategies:
            foreign_order = any(
                o["symbol"] == strategy.symbol and o["orderId"] not in own_ids for o in open_orders
            )
            mismatch = strategy.symbol not in pending_symbols and holdings.get(strategy.symbol, D(0)) != D(
                strategy.state["quantity"]
            )
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
            expired = now() - intent.created_at > timedelta(seconds=60)
            if strategy.status == "RUNNING" and not expired:
                continue
            if intent.status == "PREPARED" or intent.mode == "paper":
                async with self.sessions.begin() as session:
                    row = await session.get(Intent, intent.id)
                    row.status = "CANCELED"
            elif intent.venue == "toss" and intent.broker_id and self.settings.live_enabled:
                await self.lock.verify()
                try:
                    await self.broker.cancel(intent.broker_id)
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
                if strategy.venue == "upbit" and (not self.settings.upbit_enabled or strategy.mode == "live"):
                    strategy.reason = "업비트 모의매매 시세 연결 대기 · 실제 주문 비활성"
                    continue
                if strategy.mode == "live" and (
                    self.actual_commission is None or self.actual_commission > spec.commission_rate
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
                eligible = (
                    stock.get("currency") == "USD"
                    and stock.get("status") == "ACTIVE"
                    and (
                        stock.get("securityType") == "STOCK"
                        and stock.get("isCommonShare") is True
                        or stock.get("securityType") in {"ETF", "FOREIGN_ETF"}
                        and D(str(stock.get("leverageFactor") or "0")) == 1
                    )
                )
                if strategy.venue == "upbit":
                    market = self.upbit_markets.get(strategy.symbol)
                    eligible = market_eligible(market)
                if not eligible:
                    strategy.reason = "지원 종목·거래 상태 확인 필요"
                    continue
                quote = self.quotes.get(strategy.symbol)
                if not quote or not quote.valid(spec, at):
                    strategy.reason = "신선한 양방향 호가 또는 허용 호가 차이 조건 대기"
                    continue
                if await session.scalar(
                    select(Intent.id).where(
                        Intent.mode == strategy.mode,
                        Intent.venue == strategy.venue,
                        Intent.symbol == strategy.symbol,
                        Intent.status.in_(ACTIVE_ORDERS),
                    )
                ):
                    strategy.reason = "기존 주문 결과 확인 중"
                    continue
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
                decision, reason = decide(spec, state, quote, aggregate(bars, spec.timeframe, at))
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
                        Intent.venue == intent.venue,
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
            elif self.settings.live_enabled and intent.venue == "toss":
                await self.submit_live(intent.id)

    async def submit_live(self, intent_id, recovery=False):
        await self.lock.verify()
        async with self.sessions() as session:
            intent = await session.get(Intent, intent_id)
            strategy = await session.get(Strategy, intent.strategy_id)
            account = await session.get(AccountState, {"mode": "live", "venue": intent.venue})
            if intent.venue != "toss":
                raise BrokerError("upbit-read-only")
        if strategy.status != "RUNNING" or account.halted:
            return
        if not recovery:
            quote = self.quotes.get(intent.symbol)
            if (
                not current_session(self.calendar, datetime.now(UTC))
                or not quote
                or not quote.valid(StrategySpec.model_validate(strategy.config), datetime.now(UTC))
            ):
                return
            if intent.side == "BUY":
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
                not current_session(self.calendar, datetime.now(UTC))
                or not quote
                or not quote.valid(StrategySpec.model_validate(strategy.config), datetime.now(UTC))
            ):
                return
        await self.lock.verify()
        async with self.sessions.begin() as session:
            intent = await session.get(Intent, intent_id)
            intent.status = "SENDING"
            if intent.submitted_at is None:
                intent.submitted_at = now()
        try:
            result = await self.broker.place(intent)
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
            if venue == "upbit" and not self.settings.upbit_enabled:
                raise ValueError("업비트 시세 연결이 비활성입니다")
            broker = self.upbit if venue == "upbit" else self.broker
            if (
                payload.get("require_eligible")
                and venue == "upbit"
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
                order = await self.broker.order(command.payload["broker_id"])
                async with self.sessions.begin() as session:
                    intent = await session.get(Intent, command.payload["intent_id"])
                    if (
                        not intent
                        or intent.venue != "toss"
                        or intent.mode != "live"
                        or intent.status != "UNKNOWN"
                    ):
                        raise ValueError("대조할 미확인 실거래 주문이 없습니다")
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
