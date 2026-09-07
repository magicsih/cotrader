import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from cotrader.domain import ACTIVE_ORDERS, D, StrategySpec, apply_fill, funded_state, initial_state
from cotrader.live import MarketCautionsRequest
from cotrader.markets import VENUES, asset_key, currency, is_upbit, portfolio_key, validate_symbol
from cotrader.models import AccountState, Command, Event, Intent, Ledger, RuntimeState, Strategy, now


def approval_digest(strategy, settings) -> str:
    snapshot = [
        strategy.id,
        strategy.version,
        strategy.mode,
        strategy.config,
        str(settings.capital_for(strategy.venue)),
        settings.risk_for(strategy.venue),
        settings.live_for(strategy.venue),
        "ALL_SESSIONS_HOLD_ON_STOP",
    ]
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()[:16]


async def put_runtime(session, key, data):
    row = await session.get(RuntimeState, key)
    if row:
        row.data = data
        row.updated_at = now()
    else:
        session.add(RuntimeState(key=key, data=data))


async def bootstrap(session, settings):
    for venue in VENUES:
        for mode in ("paper", "live"):
            if not await session.get(AccountState, {"mode": mode, "venue": venue}):
                capital = settings.capital_for(venue)
                session.add(
                    AccountState(
                        mode=mode, venue=venue, capital=capital, high_water=capital, daily_anchor=capital
                    )
                )


def settlement_value(strategy):
    return D(strategy.state["cash"]) + D(strategy.state.get("released_value", "0"))


async def create_strategy(session, name: str, spec: StrategySpec, mode: str):
    if mode not in {"paper", "live"}:
        raise ValueError("지원하지 않는 실행 모드입니다")
    if len(name) > 100 or not name.strip():
        raise ValueError("전략 이름은 1~100자로 입력하세요")
    row = Strategy(
        name=name,
        symbol=spec.symbol,
        venue=spec.venue,
        config=spec.model_dump(mode="json"),
        mode=mode,
        state={**initial_state(D(0) if spec.inventory_quantity else spec.budget), "funded": False},
    )
    session.add(row)
    await session.flush()
    return row


async def enqueue(session, command_id: str, action: str, payload: dict, actor: str):
    existing = await session.get(Command, command_id)
    if existing:
        if (existing.action, existing.payload, existing.actor) != (action, payload, actor):
            raise ValueError("동일 명령 ID의 내용이 다릅니다")
        return existing
    if action not in {
        "start",
        "pause",
        "pause_all",
        "reset_risk",
        "ingest",
        "resolve",
        "archive",
        "check_live",
        "prepare_inventory",
        "set_market_cautions",
        "set_usdt_capital",
    }:
        raise ValueError("지원하지 않는 명령입니다")
    command = Command(id=command_id, action=action, payload=payload, actor=actor)
    session.add(command)
    await session.flush()
    return command


async def process_command(session, command, settings, *, live_checked=False):
    payload = command.payload
    if command.action not in {"pause", "pause_all"} and now() - command.created_at > timedelta(minutes=5):
        raise ValueError("명령이 5분 이상 지연되었습니다. 현재 상태를 확인하고 다시 요청하세요")
    if command.action == "start":
        row = await session.get(Strategy, payload["strategy_id"])
        if not row or payload.get("version") != row.version:
            raise ValueError("전략이 없거나 승인한 설정 버전이 다릅니다")
        if payload.get("approval") != approval_digest(row, settings):
            raise ValueError("승인한 설정·위험 한도가 현재 값과 다릅니다. 다시 확인하세요")
        if row.status == "ARCHIVED":
            raise ValueError("종료된 전략은 재개할 수 없습니다. 새 초안을 만드세요")
        spec = StrategySpec.model_validate(row.config)
        if is_upbit(spec.venue) and not settings.upbit_enabled:
            raise ValueError("업비트 시세 연결이 필요합니다")
        if row.mode == "live" and not settings.live_for(row.venue):
            raise ValueError("서버에서 실거래 실행을 허용하지 않았습니다")
        if row.mode == "live" and not live_checked:
            raise ValueError("실거래 시작 전 최신 계좌·주문 가능 여부 점검이 필요합니다")
        account = await session.get(AccountState, {"mode": row.mode, "venue": row.venue})
        if account.halted:
            raise ValueError("포트폴리오가 위험 중단 상태입니다. 기준 재설정 승인이 필요합니다")
        unresolved = await session.scalar(
            select(Intent).where(
                Intent.mode == row.mode, Intent.venue == row.venue, Intent.status == "UNKNOWN"
            )
        )
        if unresolved:
            raise ValueError("결과가 확인되지 않은 주문을 먼저 대조해야 합니다")
        others = (
            await session.scalars(select(Strategy).where(Strategy.id != row.id, Strategy.mode == row.mode))
        ).all()
        funded = [s for s in others if s.state.get("funded")]
        if any(asset_key(s.venue, s.symbol) == asset_key(row.venue, row.symbol) for s in funded):
            raise ValueError("같은 종목은 한 전략만 자금을 배정할 수 있습니다")
        others = [s for s in others if s.venue == row.venue]
        funded = [s for s in funded if s.venue == row.venue]
        allocated = sum((D(s.config["budget"]) for s in funded), D(0))
        settled_pnl = sum(
            (settlement_value(s) - D(s.config["budget"]) for s in others if s.state.get("settled")), D(0)
        )
        available_capital = min(account.capital, account.capital + settled_pnl)
        if not row.state.get("funded") and allocated + spec.budget > available_capital:
            raise ValueError("전략별 배정 예산 합계가 총예산을 초과합니다")
        if len(funded) >= 5:
            raise ValueError("첫 버전은 최대 5개 종목까지 동시에 운영합니다")
        state = (
            {**row.state, **funded_state(spec)}
            if spec.inventory_quantity and not row.state.get("funded")
            else dict(row.state)
        )
        state.update(funded=True, last_mid=None, last_bar=None)
        row.state, row.status, row.reason = state, "RUNNING", "시세·계좌 확인 대기"
        session.add(
            Event(
                kind="strategy",
                strategy_id=row.id,
                message=f"{row.name}: {row.mode} 전략 시작 승인",
                notify=True,
            )
        )
    elif command.action in {"pause", "pause_all"}:
        query = select(Strategy).where(Strategy.status != "ARCHIVED")
        if command.action == "pause":
            query = query.where(Strategy.id == payload["strategy_id"])
        for row in (await session.scalars(query)).all():
            row.status, row.reason = "PAUSED", "사용자 중단 — 대기 주문 취소 확인 중, 보유 유지"
        session.add(
            Event(
                kind="pause",
                message="중단 접수: 대기 주문 취소 결과를 확인합니다. 보유 자산은 유지합니다.",
                notify=True,
            )
        )
    elif command.action == "set_usdt_capital":
        from cotrader.capital import set_capital

        await set_capital(session, command, settings)
    elif command.action == "set_market_cautions":
        request = MarketCautionsRequest.model_validate(payload)
        row = await session.get(Strategy, request.strategy_id)
        if not row or not is_upbit(row.venue):
            raise ValueError("주의 설정을 변경할 업비트 전략이 없습니다")
        if request.version != row.version or request.approval != approval_digest(row, settings):
            raise ValueError("전략 설정이 변경되었습니다. 최신 주의 설정을 다시 확인하세요")
        if row.status not in {"DRAFT", "PAUSED"}:
            raise ValueError("주의 설정은 승인 대기 또는 중단된 전략에서 변경할 수 있습니다")
        if await session.scalar(
            select(Intent.id).where(Intent.strategy_id == row.id, Intent.status.in_(ACTIVE_ORDERS))
        ):
            raise ValueError("이 전략의 미체결·미확인 주문 대조가 끝난 후 주의 설정을 변경하세요")
        spec = StrategySpec.model_validate(
            {
                **row.config,
                "allowed_market_cautions": request.allowed_market_cautions,
            }
        )
        previous = row.config.get("allowed_market_cautions", [])
        if tuple(sorted(previous)) != spec.allowed_market_cautions:
            row.config = spec.model_dump(mode="json")
            row.version += 1
            session.add(
                Event(
                    kind="audit",
                    strategy_id=row.id,
                    message="전략별 주의 항목 허용 설정 변경",
                    data={
                        "actor": command.actor,
                        "version": row.version,
                        "previous": previous,
                        "allowed_market_cautions": row.config["allowed_market_cautions"],
                    },
                )
            )
    elif command.action == "archive":
        row = await session.get(Strategy, payload["strategy_id"])
        if not row or row.status == "RUNNING" or D(row.state["quantity"]) != 0:
            raise ValueError("전략을 중단하고 보유 수량이 0인지 확인한 후 종료하세요")
        if await session.scalar(
            select(Intent.id).where(Intent.strategy_id == row.id, Intent.status.in_(ACTIVE_ORDERS))
        ):
            raise ValueError("미체결 또는 미확인 주문이 남아 있습니다")
        row.state = {
            **row.state,
            "settled": bool(row.state.get("funded") or row.state.get("settled")),
            "funded": False,
        }
        row.status, row.reason = "ARCHIVED", "운영 종료 — 기록과 실현 손익 보존"
        session.add(
            Event(
                kind="strategy", strategy_id=row.id, message=f"{row.name}: 운영 종료·예산 해제", notify=True
            )
        )
    elif command.action == "reset_risk":
        if payload.get("confirm") != "RESET_ANCHORS":
            raise ValueError("새 손실 기준으로 재설정한다는 명시적 확인이 필요합니다")
        account = await session.get(
            AccountState, {"mode": payload["mode"], "venue": payload.get("venue", "toss")}
        )
        if not account:
            raise ValueError("잘못된 실행 모드입니다")
        snapshot = await session.get(RuntimeState, portfolio_key(account.venue, account.mode))
        if (
            not snapshot
            or not snapshot.data.get("complete")
            or now() - snapshot.updated_at > timedelta(seconds=30)
        ):
            raise ValueError("최신 평가금액을 확인할 수 없습니다")
        account.daily_anchor = account.high_water = D(snapshot.data["equity"])
        account.halted, account.reason = False, "사용자가 손실 기준을 현재 평가금액으로 재설정했습니다"
        session.add(
            Event(
                kind="risk",
                message=account.reason,
                data={"mode": account.mode, "anchor": str(account.high_water)},
                notify=True,
            )
        )
    elif command.action == "ingest":
        from datetime import datetime

        venue = payload.get("venue", "toss")
        if venue not in VENUES:
            raise ValueError("지원하지 않는 시장입니다")
        validate_symbol(venue, payload.get("symbol", ""))
        if is_upbit(venue) and not settings.upbit_enabled:
            raise ValueError("업비트 시세 연결이 비활성입니다")
        if payload.get("interval", "1m") not in {"1m", "1d"}:
            raise ValueError("1m 또는 1d만 지원합니다")
        start = datetime.fromisoformat(payload["from"])
        if start.tzinfo is None:
            raise ValueError("수집 시작 시각에는 시간대가 필요합니다")
        if payload.get("to"):
            end = datetime.fromisoformat(payload["to"])
            if end.tzinfo is None or end <= start:
                raise ValueError("수집 종료 시각에는 시간대가 필요하며 시작 이후여야 합니다")
        command.status, command.result = "RUNNING", {"count": 0, "pages": 0, "before": payload.get("to")}
        return
    elif command.action == "resolve":
        # Resolution needs broker readback and is performed by the engine.
        command.status = "RUNNING"
        return
    elif command.action in {"check_live", "prepare_inventory"}:
        raise ValueError("실거래 준비 점검은 엔진의 계좌 확인이 필요합니다")
    command.status, command.result = "SUCCEEDED", {"message": "반영했습니다"}


async def record_execution(session, intent: Intent, order: dict):
    if order["symbol"] != intent.symbol or order["side"] != intent.side:
        raise ValueError("증권사 주문과 내부 주문의 종목·방향이 다릅니다")
    known = {"PENDING", "PENDING_CANCEL", "PARTIAL_FILLED", "FILLED", "CANCELED", "REJECTED"}
    status = order["status"]
    if status not in known:
        intent.status, intent.reason = "UNKNOWN", f"확인이 필요한 증권사 주문 상태: {status[:50]}"
        return
    execution = order["execution"]
    quantity = D(execution["filledQuantity"])
    if quantity < intent.filled_quantity:
        return  # Reordered event. REST reconciliation will provide current state.
    if quantity > intent.quantity:
        raise ValueError("주문 수량을 초과한 체결 응답입니다")
    amount = D(execution["filledAmount"] or "0")
    if not quantity.is_finite() or quantity < 0 or not amount.is_finite() or amount < 0:
        raise ValueError("체결 수량·금액이 유효하지 않습니다")
    if quantity and not execution.get("filledAmount"):
        raise ValueError("체결 금액이 없는 응답입니다")
    row = await session.get(Strategy, intent.strategy_id)
    final = execution.get("commission") is not None and execution.get("tax") is not None
    costs = D(execution.get("commission") or str(amount * D(row.config["commission_rate"]))) + D(
        execution.get("tax") or "0"
    )
    if not costs.is_finite() or costs < 0:
        raise ValueError("체결 비용이 유효하지 않습니다")
    if intent.costs_final and quantity == intent.filled_quantity and not final:
        costs, final = intent.costs, True
    fingerprint = hashlib.sha256(
        json.dumps([intent.id, str(quantity), str(amount), str(costs)]).encode()
    ).hexdigest()
    exists = await session.scalar(select(Ledger.id).where(Ledger.fingerprint == fingerprint))
    if not exists and (
        quantity != intent.filled_quantity or amount != intent.filled_amount or costs != intent.costs
    ):
        delta_quantity, delta_amount, delta_costs = (
            quantity - intent.filled_quantity,
            amount - intent.filled_amount,
            costs - intent.costs,
        )
        state = json.loads(json.dumps(row.state))
        apply_fill(state, intent.side, delta_quantity, delta_amount, delta_costs, intent.slot)
        row.state = state
        session.add(
            Ledger(
                intent_id=intent.id,
                fingerprint=fingerprint,
                quantity=delta_quantity,
                amount=delta_amount,
                costs=delta_costs,
            )
        )
        session.add(
            Event(
                kind="fill",
                strategy_id=row.id,
                message=f"{row.mode} · {row.symbol} {intent.side} {delta_quantity}{'개' if is_upbit(row.venue) else '주'} 체결",
                data={
                    "intent_id": intent.id,
                    "quantity": str(delta_quantity),
                    "amount": str(delta_amount),
                    "costs": str(delta_costs),
                },
                notify=True,
            )
        )
    intent.filled_quantity, intent.filled_amount, intent.costs, intent.costs_final = (
        quantity,
        amount,
        costs,
        final,
    )
    if quantity == intent.quantity:
        status = "FILLED"
    if intent.status in {"FILLED", "CANCELED", "REJECTED"} and status in {"PENDING", "PARTIAL_FILLED"}:
        return
    if intent.status == "PENDING_CANCEL" and status in {"PENDING", "PARTIAL_FILLED"}:
        return
    intent.status = status


async def check_risk(session, settings, quotes, trading_day: str, venue="toss"):
    risk = settings.risk_for(venue)
    strategies = (await session.scalars(select(Strategy).where(Strategy.venue == venue))).all()
    for mode in ("paper", "live"):
        account = await session.get(AccountState, {"mode": mode, "venue": venue})
        funded = [s for s in strategies if s.mode == mode and s.state.get("funded")]
        settled = [s for s in strategies if s.mode == mode and s.state.get("settled")]
        allocated = sum((D(s.config["budget"]) for s in funded), D(0))
        cash = account.capital - allocated + sum((D(s.state["cash"]) for s in funded), D(0))
        cash += sum((D(s.state["cash"]) - D(s.config["budget"]) for s in settled), D(0))
        released = sum((D(s.state.get("released_value", "0")) for s in settled), D(0))
        closed_unrealized = sum((D(s.state.get("closed_unrealized", "0")) for s in settled), D(0))
        exposure, basis, costs, realized = D(0), D(0), D(0), D(0)
        complete = True
        for strategy in settled:
            costs += D(strategy.state["costs"])
            realized += D(strategy.state["realized"])
        for strategy in funded:
            quantity = D(strategy.state["quantity"])
            quote = quotes.get(strategy.symbol)
            spec = StrategySpec.model_validate(strategy.config)
            fresh = quote is not None and quote.fresh(spec, datetime.now(UTC))
            if (
                strategy.status == "RUNNING"
                and spec.kind == "grid"
                and not spec.inventory_quantity
                and fresh
                and quote.mid < spec.lower
            ):
                strategy.status, strategy.reason = "PAUSED", "그리드 하단 이탈 — 보유 유지"
                session.add(
                    Event(
                        kind="risk",
                        strategy_id=strategy.id,
                        message=f"{strategy.symbol}: {strategy.reason}",
                        notify=True,
                    )
                )
            if quantity:
                if not fresh:
                    complete = False
                if quote:
                    exposure += quantity * quote.bid
            basis += D(strategy.state["cost_basis"])
            costs += D(strategy.state["costs"])
            realized += D(strategy.state["realized"])
        value = cash + exposure + released
        if complete:
            if trading_day and trading_day != account.trading_day:
                account.trading_day, account.daily_anchor = trading_day, value
            account.high_water = max(account.high_water, value)
            daily_loss, dd = account.daily_anchor - value, account.high_water - value
            if not account.halted and (daily_loss >= D(risk["daily_loss"]) or dd >= D(risk["drawdown"])):
                account.halted, account.reason = True, "손실 중단 기준 도달 — 주문 취소·보유 유지"
                for strategy in funded:
                    strategy.status, strategy.reason = "PAUSED", account.reason
                session.add(Event(kind="risk", message=f"{mode}: {account.reason}", notify=True))
        pending = len(
            (
                await session.scalars(
                    select(Intent.id).where(
                        Intent.mode == mode, Intent.venue == venue, Intent.status.in_(ACTIVE_ORDERS)
                    )
                )
            ).all()
        )
        unresolved = len(
            (
                await session.scalars(
                    select(Intent.id).where(
                        Intent.mode == mode, Intent.venue == venue, Intent.status.in_(["UNKNOWN", "SENDING"])
                    )
                )
            ).all()
        )
        data = {
            "mode": mode,
            "venue": venue,
            "currency": currency(venue),
            "capital": str(account.capital),
            "cash": str(cash),
            "released_value": str(released),
            "closed_unrealized": str(closed_unrealized),
            "equity": str(value) if complete else None,
            "exposure": str(exposure) if complete else None,
            "realized_gross": str(realized),
            "costs": str(costs),
            "unrealized": str(exposure - basis + closed_unrealized) if complete else None,
            "complete": complete,
            "pending_orders": pending,
            "high_water": str(account.high_water),
            "daily_anchor": str(account.daily_anchor),
            "halted": account.halted,
            "reason": account.reason,
            "unresolved_orders": unresolved,
            "daily_loss_limit": risk["daily_loss"],
            "drawdown_limit": risk["drawdown"],
        }
        await put_runtime(session, portfolio_key(venue, mode), data)
