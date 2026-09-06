import asyncio
import copy
import threading
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from cotrader.discovery import ACTIVE_DISCOVERIES, UNIVERSES, discover, research_window
from cotrader.domain import Bar, D
from cotrader.models import CandleRow, Command, DiscoveryPlan, DiscoveryRun, Event, now, uid
from cotrader.research import ResearchCancelled


def plan_request(settings, venue, budget, preference):
    return {
        "venue": venue,
        "budget": str(budget),
        "preference": preference,
        "symbols": [r["symbol"] for r in UNIVERSES[venue]],
        "risk": settings.risk_for(venue),
        "commission_rate": "0.001",
        "slippage_bps": "10",
        "version": 1,
    }


async def create_run(session, request, actor):
    start, end = research_window(datetime.now(UTC))
    frozen = {**copy.deepcopy(request), "start": start.isoformat(), "end": end.isoformat()}
    # A completed run with the same window and assumptions is evidence to reuse, not rerun.
    previous = (
        await session.scalars(
            select(DiscoveryRun)
            .where(DiscoveryRun.venue == request["venue"], DiscoveryRun.status == "SUCCEEDED")
            .order_by(DiscoveryRun.created_at.desc())
            .limit(20)
        )
    ).all()
    existing = next(
        (r for r in previous if {k: v for k, v in r.request.items() if k != "commands"} == frozen), None
    )
    if existing:
        return existing
    commands = {}
    for symbol in request["symbols"]:
        command_id = uid()
        commands[symbol] = command_id
        session.add(
            Command(
                id=command_id,
                action="ingest",
                actor=actor,
                payload={
                    "venue": request["venue"],
                    "symbol": symbol,
                    "interval": "1m",
                    "from": start.isoformat(),
                    "to": end.isoformat(),
                    "require_eligible": True,
                },
            )
        )
    row = DiscoveryRun(venue=request["venue"], request={**frozen, "commands": commands})
    session.add(row)
    await session.flush()
    session.add(
        Event(kind="audit", message="쉬운 시작 전략 탐색 요청", data={"actor": actor, "discovery_id": row.id})
    )
    return row


async def schedule_due(settings, sessions):
    async with sessions.begin() as session:
        plans = (
            await session.scalars(
                select(DiscoveryPlan)
                .where(DiscoveryPlan.enabled.is_(True), DiscoveryPlan.next_run_at <= now())
                .with_for_update(skip_locked=True)
            )
        ).all()
        for plan in plans:
            if (plan.venue == "upbit" and not settings.upbit_enabled) or (
                plan.venue == "toss" and settings.market_source != "toss"
            ):
                plan.enabled, plan.next_run_at = False, None
                session.add(
                    Event(
                        kind="research",
                        message=f"{plan.venue}: 시세 연결 비활성으로 매일 탐색을 중지했습니다",
                        notify=True,
                    )
                )
                continue
            active = await session.scalar(
                select(DiscoveryRun.id).where(
                    DiscoveryRun.venue == plan.venue, DiscoveryRun.status.in_(ACTIVE_DISCOVERIES)
                )
            )
            if active:
                continue
            # Re-freeze current server risk limits; never increase budget or change existing strategies.
            request = {**plan.request, "risk": settings.risk_for(plan.venue)}
            if request["budget"] and D(request["budget"]) > settings.capital_for(plan.venue):
                plan.enabled, plan.next_run_at = False, None
                continue
            await create_run(session, request, "scheduler:daily")
            plan.next_run_at = now() + timedelta(days=1)


async def collecting_run(settings, sessions):
    async with sessions.begin() as session:
        rows = (
            await session.scalars(
                select(DiscoveryRun)
                .where(DiscoveryRun.status == "COLLECTING")
                .order_by(DiscoveryRun.created_at)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for row in rows:
            collections = []
            for symbol, command_id in row.request["commands"].items():
                c = await session.get(Command, command_id)
                collections.append(
                    {
                        "symbol": symbol,
                        "status": c.status if c else "FAILED",
                        "count": c.result.get("count", 0) if c else 0,
                        "message": c.result.get("message", "") if c else "수집 요청을 찾을 수 없습니다",
                    }
                )
            row.progress = {"stage": "실제 시세 수집", "collections": collections}
            if now() - row.created_at > timedelta(seconds=settings.discovery_collection_seconds):
                row.status, row.result = (
                    "FAILED",
                    {"message": "시세 수집 제한 시간을 넘었습니다. 연결 상태를 확인하세요"},
                )
                for cid in row.request["commands"].values():
                    c = await session.get(Command, cid, with_for_update=True)
                    if c and c.status in {"QUEUED", "RUNNING"}:
                        c.status = "CANCELED"
                continue
            if any(c["status"] in {"QUEUED", "RUNNING"} for c in collections):
                continue
            row.status = "RUNNING"
            return row
    return None


async def compute_discovery(settings, sessions, row, lock):
    start = datetime.fromisoformat(row.request["start"]).replace(tzinfo=None)
    end = datetime.fromisoformat(row.request["end"]).replace(tzinfo=None)
    datasets, count = {}, 0
    async with sessions() as session:
        for symbol in row.request["symbols"]:
            command = await session.get(Command, row.request["commands"][symbol])
            if not command or command.status != "SUCCEEDED":
                datasets[symbol] = {
                    "error": command.result.get("message", "시세 수집을 완료하지 못함")
                    if command
                    else "수집 요청 없음"
                }
                continue
            rows = (
                await session.scalars(
                    select(CandleRow)
                    .where(
                        CandleRow.venue == row.venue,
                        CandleRow.symbol == symbol,
                        CandleRow.interval == "1m",
                        CandleRow.timestamp >= start,
                        CandleRow.timestamp < end,
                    )
                    .order_by(CandleRow.timestamp)
                    .limit(100001)
                )
            ).all()
            count += len(rows)
            if count > 100000:
                raise ValueError("쉬운 시작의 전체 계산은 100,000봉까지 가능합니다")
            datasets[symbol] = {
                "bars": [Bar.parse(r.data) for r in rows],
                "sources": sorted({r.source for r in rows}),
            }
    stopped = threading.Event()
    deadline = time.monotonic() + settings.discovery_compute_seconds
    progress = {"stage": "전략 비교 준비", "percent": 0}

    def update(value):
        nonlocal progress
        progress = value

    task = asyncio.create_task(
        asyncio.to_thread(
            discover,
            row.request,
            datasets,
            progress=update,
            stop_requested=lambda: stopped.is_set() or time.monotonic() > deadline,
        )
    )
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=2)
            await lock.verify()
            async with sessions.begin() as session:
                current = await session.get(DiscoveryRun, row.id, with_for_update=True)
                if current.status == "CANCEL_REQUESTED":
                    stopped.set()
                else:
                    current.progress = progress
        result = await task
        if stopped.is_set():
            raise ResearchCancelled()
        return result
    except ResearchCancelled:
        if time.monotonic() > deadline:
            raise ValueError("계산 제한 시간을 넘었습니다. 시도 횟수는 자동으로 늘리지 않습니다") from None
        raise
    finally:
        stopped.set()
        if not task.done():
            try:
                await task
            except ResearchCancelled:
                pass


async def discovery_step(settings, sessions, lock):
    await schedule_due(settings, sessions)
    row = await collecting_run(settings, sessions)
    if not row:
        return False
    try:
        result = await compute_discovery(settings, sessions, row, lock)
        status = "SUCCEEDED"
    except ResearchCancelled:
        result, status = {"message": "탐색을 중단했습니다"}, "CANCELED"
    except (ValueError, KeyError, ArithmeticError) as exc:
        result, status = {"message": str(exc)}, "FAILED"
    async with sessions.begin() as session:
        current = await session.get(DiscoveryRun, row.id, with_for_update=True)
        if current.status == "CANCEL_REQUESTED":
            result, status = {"message": "탐색을 중단했습니다"}, "CANCELED"
        current.result, current.status = result, status
        selected = result.get("selected", {}).get("spec", {})
        message = f"{row.venue}: 전략 탐색 {status}"
        if selected:
            message += f" · {selected['symbol']} · " + (
                "모의 운용 검토 가능" if result["recommendation_ready"] else "추천 보류"
            )
        session.add(Event(kind="research", message=message, data={"discovery_id": row.id}, notify=True))
    return True
