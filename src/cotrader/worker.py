import asyncio
import threading
from datetime import UTC, datetime

from sqlalchemy import select

from cotrader.db import SingleWriter, database
from cotrader.domain import Bar, StrategySpec
from cotrader.models import BacktestJob, CandleRow, Event
from cotrader.research import OptimizationOptions, ResearchCancelled, backtest, optimize_grid


async def compute_job(settings, sessions, job, lock):
    spec = StrategySpec.model_validate(job.request["spec"])
    start = datetime.fromisoformat(job.request["start"]).astimezone(UTC).replace(tzinfo=None)
    end = datetime.fromisoformat(job.request["end"]).astimezone(UTC).replace(tzinfo=None)
    async with sessions() as session:
        rows = (
            await session.scalars(
                select(CandleRow)
                .where(
                    CandleRow.symbol == spec.symbol,
                    CandleRow.interval == "1m",
                    CandleRow.timestamp >= start,
                    CandleRow.timestamp < end,
                )
                .order_by(CandleRow.timestamp)
                .limit(100001)
            )
        ).all()
    if len(rows) > 100000:
        raise ValueError("한 번에 100,000봉까지 검증합니다. 기간을 나누어 요청하세요")
    bars = [Bar.parse(row.data) for row in rows]
    sources = sorted({row.source for row in rows})
    synthetic = "synthetic" in sources
    stopped = threading.Event()
    progress = {"stage": "검증 준비", "percent": 0}

    def on_progress(value):
        nonlocal progress
        progress = value

    args = dict(
        daily_loss=settings.daily_loss_usd, drawdown=settings.drawdown_usd, stop_requested=stopped.is_set
    )
    if job.request.get("action") == "suggest":
        work = asyncio.to_thread(
            optimize_grid,
            spec,
            bars,
            synthetic=synthetic,
            progress=on_progress,
            options=OptimizationOptions.model_validate(job.request.get("optimization", {})),
            **args,
        )
    else:
        work = asyncio.to_thread(backtest, spec, bars, **args)
    task = asyncio.create_task(work)
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=2)
            await lock.verify()
            async with sessions.begin() as session:
                row = await session.get(BacktestJob, job.id, with_for_update=True)
                if row.status == "CANCEL_REQUESTED":
                    stopped.set()
                elif not task.done():
                    row.result = {"progress": progress, "synthetic": synthetic}
        result = await task
        if stopped.is_set():
            raise ResearchCancelled()
        return {
            **result,
            "sources": sources,
            "synthetic": synthetic,
            "requested_start": job.request["start"],
            "requested_end": job.request["end"],
        }
    finally:
        stopped.set()
        if not task.done():
            try:
                await task
            except ResearchCancelled:
                pass


async def run_worker(settings):
    db, sessions = database(settings)
    try:
        async with SingleWriter(db, "cotrader:research") as lock:
            async with sessions.begin() as session:
                for row in (
                    await session.scalars(
                        select(BacktestJob).where(BacktestJob.status.in_(["RUNNING", "CANCEL_REQUESTED"]))
                    )
                ).all():
                    row.status, row.result = (
                        "FAILED",
                        {"message": "프로세스가 재시작되었습니다. 동일 설정으로 새 검증을 요청하세요"},
                    )
            while True:
                await lock.verify()
                async with sessions.begin() as session:
                    job = await session.scalar(
                        select(BacktestJob)
                        .where(BacktestJob.status == "QUEUED")
                        .order_by(BacktestJob.created_at)
                        .with_for_update(skip_locked=True)
                    )
                    if job:
                        job.status = "RUNNING"
                if not job:
                    await asyncio.sleep(2)
                    continue
                try:
                    result = await compute_job(settings, sessions, job, lock)
                    state = "SUCCEEDED"
                except ResearchCancelled:
                    result, state = {"message": "검증 작업을 중단했습니다"}, "CANCELED"
                except (ValueError, KeyError, ArithmeticError) as exc:
                    result, state = {"message": str(exc)}, "FAILED"
                async with sessions.begin() as session:
                    row = await session.get(BacktestJob, job.id, with_for_update=True)
                    if row.status == "CANCEL_REQUESTED":
                        result, state = {"message": "검증 작업을 중단했습니다"}, "CANCELED"
                    row.status, row.result = state, result
                    session.add(
                        Event(
                            kind="backtest",
                            message=f"{job.request['spec']['symbol']}: 검증 작업 {state}",
                            data={"job_id": job.id},
                            notify=True,
                        )
                    )
    finally:
        await db.dispose()
