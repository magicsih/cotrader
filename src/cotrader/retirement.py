"""Release strategy accounting after a verified in-kind pocket return; never sell assets."""

from sqlalchemy import select

from cotrader.domain import ACTIVE_ORDERS, D, StrategySpec
from cotrader.markets import UPBIT_VENUES, currency
from cotrader.models import AccountState, Command, Event, Intent, Strategy
from cotrader.pockets import digest, require, validate_plan
from cotrader.services import RISK_HALT_REASONS


async def retire_returned(session, plan, quotes, at):
    validate_plan(plan)
    require(plan["schema"] == 2, "메인포켓 반환 계획으로만 운용 종료할 수 있습니다")
    require(
        not await session.scalar(select(Command.id).where(Command.status.in_(["QUEUED", "RUNNING"]))),
        "처리 중 명령이 있습니다",
    )
    require(
        not await session.scalar(
            select(Intent.id).where(Intent.mode == "live", Intent.status.in_(ACTIVE_ORDERS))
        ),
        "미체결·미확인 주문이 남아 있습니다",
    )
    rows = (
        await session.scalars(
            select(Strategy)
            .where(Strategy.mode == "live", Strategy.venue.in_(UPBIT_VENUES))
            .order_by(Strategy.id)
            .with_for_update()
        )
    ).all()
    current = [
        {
            "id": r.id,
            "symbol": r.symbol,
            "status": r.status,
            "version": r.version,
            "config": r.config,
            "state": r.state,
        }
        for r in rows
    ]
    require(current == plan["ledger"], "반환 계획 이후 전략 장부가 변경됐습니다")
    prepared = []
    for row in rows:
        if row.status == "ARCHIVED":
            continue
        require(row.status in {"PAUSED", "DRAFT"}, "전략을 중단한 후 운용 종료하세요")
        quantity = D(row.state["quantity"])
        value = D(0)
        if quantity:
            spec = StrategySpec.model_validate(row.config)
            quote = quotes.get(row.symbol)
            require(quote is not None and quote.valid(spec, at), "종료 평가에 필요한 최신 호가가 없습니다")
            value = quantity * quote.bid
        prepared.append((row, value))
    result = []
    for row, value in prepared:
        before = dict(row.state)
        row.state = {
            **before,
            "funded": False,
            "settled": bool(before.get("funded") or before.get("settled")),
            "quantity": "0",
            "cost_basis": "0",
            "lots": {},
            "released_quantity": before["quantity"],
            "released_value": str(value),
            "released_currency": currency(row.venue),
            "closed_unrealized": str(value - D(before["cost_basis"])),
            "retirement_plan_sha256": digest(plan),
        }
        row.version += 1
        row.status = "ARCHIVED"
        row.reason = "메인포켓 반환 완료 — 매도 없이 운용 종료·이력 보존"
        session.add(
            Event(
                kind="retirement",
                strategy_id=row.id,
                message=row.reason,
                data={
                    "plan_sha256": digest(plan),
                    "source_state": before,
                    "released_value": str(value),
                    "currency": currency(row.venue),
                    "valued_at": at.isoformat(),
                    "actual_order_created": False,
                    "realized_pnl_unchanged": True,
                },
            )
        )
        result.append(
            {
                "strategy_id": row.id,
                "status": row.status,
                "released_quantity": before["quantity"],
                "released_value": str(value),
            }
        )
    return result


async def reactivate_returned(session, plan, at):
    """Restore one retired strategy after its exact in-kind balances returned to its pocket."""
    validate_plan(plan)
    require(plan["schema"] == 3, "선택 재개 계획으로만 전략을 복원할 수 있습니다")
    require(
        not await session.scalar(select(Command.id).where(Command.status.in_(["QUEUED", "RUNNING"]))),
        "처리 중 명령이 있습니다",
    )
    require(
        not await session.scalar(
            select(Intent.id).where(Intent.mode == "live", Intent.status.in_(ACTIVE_ORDERS))
        ),
        "미체결·미확인 주문이 남아 있습니다",
    )
    rows = (
        await session.scalars(
            select(Strategy)
            .where(Strategy.mode == "live", Strategy.venue.in_(UPBIT_VENUES))
            .order_by(Strategy.id)
            .with_for_update()
        )
    ).all()
    current = [
        {
            "id": row.id,
            "symbol": row.symbol,
            "status": row.status,
            "version": row.version,
            "config": row.config,
            "state": row.state,
        }
        for row in rows
    ]
    require(current == plan["ledger"], "재개 계획 이후 전략 장부가 변경됐습니다")
    evidence = plan["reactivation"]
    matches = [row for row in rows if row.id == evidence["strategy_id"]]
    require(len(matches) == 1, "재개 대상 전략이 없습니다")
    row = matches[0]
    require(
        row.status == "ARCHIVED" and row.venue == "upbit_usdt" and row.symbol == evidence["symbol"],
        "재개 대상 전략 상태가 다릅니다",
    )
    require(
        row.state.get("retirement_plan_sha256") == evidence["retirement_plan_sha256"],
        "전략의 종료 계획 해시가 다릅니다",
    )
    event = await session.get(Event, evidence["retirement_event_id"], with_for_update=True)
    require(
        event
        and event.kind == "retirement"
        and event.strategy_id == row.id
        and event.data.get("plan_sha256") == evidence["retirement_plan_sha256"]
        and event.data.get("source_state") == evidence["source_state"]
        and event.data.get("actual_order_created") is False,
        "종료 이벤트 원본과 재개 계획이 다릅니다",
    )
    source_state = evidence["source_state"]
    require(
        source_state.get("funded") is True and D(source_state["quantity"]) > 0,
        "복원할 자금 배정 상태가 아닙니다",
    )
    require(
        not any(other.id != row.id and other.state.get("funded") for other in rows),
        "다른 업비트 실거래 전략에 자금이 배정되어 있습니다",
    )
    account = await session.get(AccountState, {"venue": "upbit_usdt", "mode": "live"}, with_for_update=True)
    require(
        account and account.halted and account.reason in RISK_HALT_REASONS,
        "기존 평가금액 하락 중단 상태에서만 자동 복구 대기로 전환할 수 있습니다",
    )
    row.state = copy_state = dict(source_state)
    row.version += 1
    row.status = "PAUSED"
    row.reason = account.reason
    plan_hash = digest(plan)
    session.add(
        Event(
            kind="reactivation",
            strategy_id=row.id,
            message="포켓 원상 복귀 확인 — 기존 평가 기준점 유지·자동 복구 대기",
            data={
                "plan_sha256": plan_hash,
                "retirement_event_id": event.id,
                "restored_state": copy_state,
                "reactivated_at": at.isoformat(),
                "actual_order_created": False,
                "anchors_reset": False,
            },
        )
    )
    return {
        "strategy_id": row.id,
        "status": row.status,
        "reason": row.reason,
        "plan_sha256": plan_hash,
        "anchors_reset": False,
    }
