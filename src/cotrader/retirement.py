"""Release strategy accounting after a verified in-kind pocket return; never sell assets."""

from sqlalchemy import select

from cotrader.domain import ACTIVE_ORDERS, D, StrategySpec
from cotrader.markets import UPBIT_VENUES, currency
from cotrader.models import Command, Event, Intent, Strategy
from cotrader.pockets import digest, require, validate_plan


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
