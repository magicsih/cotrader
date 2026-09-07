"""Operator-confirmed USDT allocation limits, separate from strategy execution."""

import hashlib
import json
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from cotrader.domain import D
from cotrader.models import AccountState, Event, Strategy


class CapitalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capital: Decimal = Field(gt=0, le=10000, decimal_places=10, allow_inf_nan=False)
    approval: str = Field(pattern=r"^[a-f0-9]{16}$")


async def capital_view(session, settings):
    account = await session.get(AccountState, {"venue": "upbit_usdt", "mode": "live"})
    if not account:
        raise ValueError("USDT 실거래 계좌 초기화를 기다려주세요")
    rows = list(
        await session.scalars(
            select(Strategy)
            .where(Strategy.venue == "upbit_usdt", Strategy.mode == "live")
            .order_by(Strategy.id)
        )
    )
    active = [row for row in rows if row.status != "ARCHIVED"]
    settled_pnl = sum(
        (
            D(row.state["cash"]) + D(row.state.get("released_value", 0)) - D(row.config["budget"])
            for row in rows
            if row.state.get("settled")
        ),
        D(0),
    )
    allocated = sum((D(row.config["budget"]) for row in active if row.state.get("funded")), D(0))
    total = sum((D(row.config["budget"]) for row in active), D(0))
    strategies = [
        {
            "id": row.id,
            "version": row.version,
            "symbol": row.symbol,
            "status": row.status,
            "budget": row.config["budget"],
            "funded": bool(row.state.get("funded")),
        }
        for row in active
    ]
    snapshot = [
        str(account.capital),
        str(settled_pnl),
        strategies,
        settings.risk_for("upbit_usdt"),
        settings.live_for("upbit_usdt"),
    ]
    return {
        "capital": str(account.capital),
        "minimum": str(allocated - min(settled_pnl, D(0))),
        "required": str(total - min(settled_pnl, D(0))),
        "maximum": "10000",
        "strategies": strategies,
        "risk": settings.risk_for("upbit_usdt"),
        "approval": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()[:16],
    }


async def set_capital(session, command, settings):
    request = CapitalRequest.model_validate(command.payload)
    preview = await capital_view(session, settings)
    if request.approval != preview["approval"]:
        raise ValueError("계좌 한도나 전략이 변경되었습니다. 운용 한도 설정을 다시 열어주세요")
    if request.capital < D(preview["minimum"]):
        raise ValueError("현재 배정 예산과 확정 손익을 감안한 최소 한도보다 작습니다")
    account = await session.get(AccountState, {"venue": "upbit_usdt", "mode": "live"})
    delta = request.capital - account.capital
    if not delta:
        return
    previous = str(account.capital)
    account.capital = request.capital
    # Shift both anchors equally so an allocation limit change cannot erase losses.
    account.high_water += delta
    account.daily_anchor += delta
    for item in preview["strategies"]:
        row = await session.get(Strategy, item["id"])
        row.version += 1
    session.add(
        Event(
            kind="audit",
            message="USDT 실거래 운용 한도 변경",
            data={
                "actor": command.actor,
                "previous": previous,
                "capital": str(request.capital),
                "strategy_versions_invalidated": [item["id"] for item in preview["strategies"]],
            },
            notify=True,
        )
    )
