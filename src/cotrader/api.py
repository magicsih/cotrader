import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from cotrader.auth import require_actor, session_token, telegram_user
from cotrader.config import Settings
from cotrader.db import database
from cotrader.domain import Bar, StrategySpec, grid_quantity, levels
from cotrader.markets import Venue, currency, is_upbit, portfolio_key, validate_symbol
from cotrader.models import (
    AccountState,
    BacktestJob,
    CandleRow,
    Command,
    Event,
    Intent,
    RuntimeState,
    Snapshot,
    Strategy,
)
from cotrader.profit import ReportCurrency, profit_summary
from cotrader.research import OptimizationOptions
from cotrader.services import approval_digest, create_strategy, enqueue


class StrategyInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    mode: Literal["paper", "live"] = "paper"
    spec: StrategySpec


class CommandInput(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_\-:]{1,64}$")
    action: str
    payload: dict = Field(default_factory=dict)


class ResearchInput(BaseModel):
    spec: StrategySpec
    start: AwareDatetime
    end: AwareDatetime
    action: Literal["backtest", "suggest"] = "backtest"
    optimization: OptimizationOptions = Field(default_factory=OptimizationOptions)


class CandleImport(BaseModel):
    venue: Venue = "toss"
    symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.\-]{0,23}$")
    source: Literal["user", "synthetic"] = "user"
    candles: list[dict] = Field(min_length=3, max_length=20000)


Actor = Annotated[str, Depends(require_actor)]


def serialize(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def create_app(settings: Settings | None = None, sessions_override=None):
    settings = settings or Settings()
    db, sessions = database(settings)
    if sessions_override is not None:
        sessions = sessions_override

    @asynccontextmanager
    async def lifespan(app):
        task = None
        if settings.telegram_enabled or settings.auth_mode == "telegram":
            from cotrader.telegram import TelegramBot

            task = asyncio.create_task(TelegramBot(settings, sessions, db).run())
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await db.dispose()

    app = FastAPI(
        title="Cotrader", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.settings = settings
    from cotrader.github_auth import github_routes

    app.include_router(github_routes(settings, sessions))
    from cotrader.research_routes import research_routes

    app.include_router(research_routes(sessions, serialize))
    from cotrader.discovery_routes import discovery_routes

    app.include_router(discovery_routes(settings, sessions, serialize))

    @app.get("/api/paper-lab")
    async def paper_lab(_actor: Actor):
        from datetime import UTC, datetime

        from cotrader.paper_lab import report

        async with sessions() as session:
            result = await report(session, datetime.now(UTC))
            runtime = await session.get(RuntimeState, "paper_lab")
        return {**result, "enabled": settings.paper_lab_enabled, "runtime": runtime.data if runtime else None}

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def value_error(_request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(IntegrityError)
    async def conflict(_request, _exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"detail": "이미 처리된 요청 또는 중복 데이터입니다"})

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        try:
            async with sessions() as session:
                await session.execute(text("SELECT 1"))
                await session.scalar(select(Strategy.id).limit(1))
        except Exception as exc:
            raise HTTPException(503, "DB 연결 또는 마이그레이션 확인 필요") from exc
        return {"status": "ok"}

    @app.get("/api/auth/mode")
    async def auth_mode():
        return {"mode": settings.auth_mode}

    @app.get("/api/auth/session")
    async def auth_session(actor: Actor):
        return {"actor": actor}

    @app.post("/api/auth/logout")
    async def logout(response: Response, _actor: Actor):
        response.delete_cookie("cotrader_session", path="/", secure=True, httponly=True, samesite="lax")
        return {"ok": True}

    @app.post("/api/auth/telegram")
    async def authenticate(request: Request, response: Response):
        if settings.auth_mode != "telegram" or request.headers.get("origin") != settings.public_url:
            raise HTTPException(403, "Telegram 인증 요청을 허용하지 않습니다")
        body = await request.json()
        data = body.get("init_data", "")
        if not isinstance(data, str) or len(data) > 10000:
            raise HTTPException(400, "인증 데이터가 유효하지 않습니다")
        try:
            user = telegram_user(data, settings.telegram_token.get_secret_value(), settings.telegram_me)
        except (ValueError, KeyError) as exc:
            raise HTTPException(401, "Telegram 인증 실패") from exc
        response.set_cookie(
            "cotrader_session",
            session_token(user, settings.session_secret.get_secret_value()),
            httponly=True,
            secure=True,
            samesite="strict",
            max_age=3600,
            path="/",
        )
        return {"ok": True}

    @app.get("/api/status")
    async def status(_actor: Actor, venue: Venue = "toss", mode: Literal["paper", "live"] = "paper"):
        async with sessions() as session:
            runtime = await session.get(RuntimeState, "engine:upbit" if is_upbit(venue) else "engine")
            telegram = await session.get(RuntimeState, "telegram")
            account = await session.get(AccountState, {"venue": venue, "mode": mode})
            return {
                "engine": serialize(runtime) if runtime else None,
                "live_enabled": settings.live_for(venue),
                "venue": venue,
                "currency": currency(venue),
                "capital": str(account.capital if account else settings.capital_for(venue)),
                "daily_loss": settings.risk_for(venue)["daily_loss"],
                "drawdown": settings.risk_for(venue)["drawdown"],
                "auto_recover": settings.auto_recover_for(venue),
                "risk_recovery_ratio": str(settings.risk_recovery_ratio),
                "risk_recovery_seconds": settings.risk_recovery_seconds,
                "market_source": ("upbit" if settings.upbit_enabled else "offline")
                if is_upbit(venue)
                else settings.market_source,
                "telegram": serialize(telegram) if telegram else None,
            }

    @app.get("/api/account")
    async def account(_actor: Actor, venue: Venue = "toss"):
        from cotrader.account import account_view

        async with sessions() as session:
            data = account_view(
                await session.get(RuntimeState, "upbit_account" if is_upbit(venue) else "broker_account"),
                venue,
            )
            if is_upbit(venue):
                data["read_only"] = not settings.live_for(venue)
            return data

    @app.get("/api/portfolio")
    async def portfolio(_actor: Actor, mode: Literal["paper", "live"] = "paper", venue: Venue = "toss"):
        async with sessions() as session:
            row = await session.get(RuntimeState, portfolio_key(venue, mode))
            return serialize(row) if row else None

    @app.get("/api/profit")
    async def profit(
        _actor: Actor, mode: Literal["paper", "live"] = "live", base_currency: ReportCurrency = "KRW"
    ):
        async with sessions() as session:
            return await profit_summary(session, mode, base_currency)

    @app.get("/api/strategies")
    async def strategies(_actor: Actor):
        async with sessions() as session:
            etf_history = await session.get(RuntimeState, "etf_rotation_history")
            etf_readiness = (
                {
                    "last_session": etf_history.data["last_session"],
                    "checked_at": etf_history.data["checked_at"],
                    "source": etf_history.data["source"],
                    "bars": {s: len(rows) for s, rows in etf_history.data["series"].items()},
                }
                if etf_history
                else None
            )
            if etf_history:
                from cotrader.rotation import monthly_targets

                try:
                    etf_readiness["reference_targets"] = monthly_targets(
                        etf_history.data["series"], etf_history.data["last_session"]
                    )[0]
                except (ValueError, KeyError, ArithmeticError):
                    etf_readiness["reference_targets"] = None
            pending = {
                strategy_id
                for payload in await session.scalars(
                    select(Command.payload).where(
                        Command.action == "set_market_cautions",
                        Command.status.in_(["QUEUED", "RUNNING"]),
                    )
                )
                if isinstance(strategy_id := payload.get("strategy_id"), str)
            }
            capital_pending = await session.scalar(
                select(Command.id).where(
                    Command.action == "set_usdt_capital", Command.status.in_(["QUEUED", "RUNNING"])
                )
            )
            return [
                {
                    **serialize(row),
                    "etf_data": etf_readiness if row.config["kind"] == "rotation" else None,
                    "approval": approval_digest(row, settings),
                    "pending_settings": row.id in pending
                    or bool(capital_pending and row.venue == "upbit_usdt" and row.mode == "live"),
                }
                for row in (
                    await session.scalars(select(Strategy).order_by(Strategy.created_at.desc()))
                ).all()
            ]

    @app.get("/api/upbit-usdt-capital")
    async def upbit_usdt_capital(_actor: Actor):
        from cotrader.capital import capital_view

        async with sessions() as session:
            return await capital_view(session, settings)

    @app.post("/api/strategies/preview")
    async def preview(body: StrategyInput, _actor: Actor):
        from cotrader.rotation import summary

        return {
            "spec": body.spec.model_dump(mode="json"),
            "grid": [
                {
                    "buy": str(p),
                    "sell": str(levels(body.spec)[i + 1]),
                    "quantity": str(grid_quantity(body.spec, i)),
                }
                for i, p in enumerate(levels(body.spec)[:-1])
            ]
            if body.spec.kind == "grid"
            else [],
            "maximum_budget": str(body.spec.budget),
            "mode": body.mode,
            "loss_action": "대기 주문 취소·보유 유지·알림",
            "sessions": "미국 정규장"
            if body.spec.kind == "rotation"
            else "24시간"
            if is_upbit(body.spec.venue)
            else "토스 지원 전체 세션",
            "rotation": summary() if body.spec.kind == "rotation" else None,
            "currency": currency(body.spec.venue),
            "costs": "모의 수수료와 체결 비용은 설정값이며 실제 비용과 다를 수 있습니다",
        }

    @app.post("/api/strategies", status_code=201)
    async def add_strategy(body: StrategyInput, actor: Actor):
        async with sessions.begin() as session:
            row = await create_strategy(session, body.name, body.spec, body.mode)
            session.add(
                Event(
                    kind="audit",
                    strategy_id=row.id,
                    message="전략 설정 저장",
                    data={"actor": actor, "version": row.version},
                )
            )
            return serialize(row)

    @app.post("/api/commands", status_code=202)
    async def command(body: CommandInput, actor: Actor):
        async with sessions.begin() as session:
            row = await enqueue(session, body.id, body.action, body.payload, actor)
            return serialize(row)

    @app.get("/api/commands")
    async def commands(_actor: Actor):
        async with sessions() as session:
            return [
                serialize(row)
                for row in (
                    await session.scalars(select(Command).order_by(Command.created_at.desc()).limit(30))
                ).all()
            ]

    @app.get("/api/commands/{command_id}")
    async def command_status(command_id: str, _actor: Actor):
        async with sessions() as session:
            row = await session.get(Command, command_id)
            if not row:
                raise HTTPException(404, "요청 기록이 없습니다")
            return serialize(row)

    @app.get("/api/orders")
    async def orders(_actor: Actor, mode: Literal["paper", "live"] = "paper", venue: Venue = "toss"):
        async with sessions() as session:
            return [
                serialize(row)
                for row in (
                    await session.scalars(
                        select(Intent)
                        .where(Intent.mode == mode, Intent.venue == venue)
                        .order_by(Intent.created_at.desc())
                        .limit(200)
                    )
                ).all()
            ]

    @app.get("/api/events")
    async def events(_actor: Actor):
        async with sessions() as session:
            return [
                serialize(row)
                for row in (
                    await session.scalars(select(Event).order_by(Event.created_at.desc()).limit(100))
                ).all()
            ]

    @app.get("/api/snapshots")
    async def snapshots(_actor: Actor, mode: Literal["paper", "live"] = "paper", venue: Venue = "toss"):
        async with sessions() as session:
            rows = (
                await session.scalars(
                    select(Snapshot)
                    .where(Snapshot.mode == mode, Snapshot.venue == venue)
                    .order_by(Snapshot.created_at.desc())
                    .limit(500)
                )
            ).all()
            return [serialize(row) for row in reversed(rows)]

    @app.get("/api/datasets")
    async def datasets(_actor: Actor):
        async with sessions() as session:
            rows = (
                await session.execute(
                    select(
                        CandleRow.venue,
                        CandleRow.symbol,
                        CandleRow.interval,
                        CandleRow.source,
                        func.count(),
                        func.min(CandleRow.timestamp),
                        func.max(CandleRow.timestamp),
                    ).group_by(CandleRow.venue, CandleRow.symbol, CandleRow.interval, CandleRow.source)
                )
            ).all()
            return [
                dict(
                    zip(("venue", "symbol", "interval", "source", "count", "first", "last"), row, strict=True)
                )
                for row in rows
            ]

    @app.post("/api/datasets/import", status_code=201)
    async def import_candles(body: CandleImport, actor: Actor):
        validate_symbol(body.venue, body.symbol)
        bars = [Bar.parse(value) for value in body.candles]
        if len({b.at for b in bars}) != len(bars):
            raise ValueError("중복 시각의 봉이 있습니다")
        for bar in bars:
            if (
                bar.at.second
                or bar.at.microsecond
                or not (0 < bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high)
                or bar.volume < 0
            ):
                raise ValueError("1분봉 시각·가격·거래량이 올바르지 않습니다")
        async with sessions.begin() as session:
            for bar in bars:
                stamp = bar.at.replace(tzinfo=None)
                exists = await session.scalar(
                    select(CandleRow.id).where(
                        CandleRow.symbol == body.symbol,
                        CandleRow.venue == body.venue,
                        CandleRow.interval == "1m",
                        CandleRow.timestamp == stamp,
                    )
                )
                if exists:
                    raise ValueError("기존 시세와 겹칩니다. 기존 데이터를 덮어쓰지 않습니다")
                session.add(
                    CandleRow(
                        symbol=body.symbol,
                        venue=body.venue,
                        interval="1m",
                        timestamp=stamp,
                        data=bar.json(),
                        source=body.source,
                    )
                )
            session.add(
                Event(
                    kind="audit",
                    message=f"{body.symbol} 데이터 {len(bars)}봉 등록",
                    data={"actor": actor, "source": body.source},
                )
            )
        return {"count": len(bars)}

    @app.post("/api/backtests", status_code=202)
    async def add_backtest(body: ResearchInput, _actor: Actor):
        if body.start >= body.end:
            raise ValueError("시작 시각은 종료 시각보다 빨라야 합니다")
        async with sessions.begin() as session:
            payload = body.model_dump(mode="json")
            payload["assumptions"] = settings.risk_for(body.spec.venue)
            if body.action == "suggest":
                payload["optimizer_version"] = 2
            row = BacktestJob(request=payload)
            session.add(row)
            await session.flush()
            return serialize(row)

    @app.get("/api/backtests")
    async def backtests(_actor: Actor):
        async with sessions() as session:
            rows = (
                await session.scalars(select(BacktestJob).order_by(BacktestJob.created_at.desc()).limit(30))
            ).all()
            return [
                {
                    **serialize(row),
                    "result": {k: v for k, v in row.result.items() if k not in {"curve", "trades"}},
                }
                for row in rows
            ]

    @app.post("/api/backtests/{job_id}/cancel")
    async def cancel_backtest(job_id: str, actor: Actor):
        async with sessions.begin() as session:
            row = await session.get(BacktestJob, job_id, with_for_update=True)
            if not row:
                raise HTTPException(404, "검증 작업을 찾을 수 없습니다")
            if row.status in {"QUEUED", "RUNNING"}:
                row.status = "CANCELED" if row.status == "QUEUED" else "CANCEL_REQUESTED"
                session.add(
                    Event(
                        kind="audit", message="검증 작업 중단 요청", data={"job_id": job_id, "actor": actor}
                    )
                )
            return serialize(row)

    @app.get("/api/backtests/{job_id}")
    async def backtest_result(job_id: str, _actor: Actor):
        async with sessions() as session:
            row = await session.get(BacktestJob, job_id)
            if not row:
                raise HTTPException(404, "작업을 찾을 수 없습니다")
            return serialize(row)

    static_dir = Path(settings.static_dir)
    if (static_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/guide")
    @app.get("/")
    async def index():
        if not (static_dir / "index.html").is_file():
            raise HTTPException(503, "웹뷰를 먼저 빌드하세요: pnpm --dir web build")
        return FileResponse(static_dir / "index.html")

    return app
