from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def uid() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Strategy(Base):
    venue: Mapped[str] = mapped_column(String(12), default="toss", index=True)
    __tablename__ = "strategies"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100))
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    mode: Mapped[str] = mapped_column(String(12), default="paper")
    status: Mapped[str] = mapped_column(String(24), default="DRAFT")
    version: Mapped[int] = mapped_column(Integer, default=1)
    config: Mapped[dict] = mapped_column(JSON)
    state: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(String(300), default="승인 대기")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Intent(Base):
    venue: Mapped[str] = mapped_column(String(12), default="toss", index=True)
    __tablename__ = "order_intents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    strategy_id: Mapped[str] = mapped_column(String(36), index=True)
    mode: Mapped[str] = mapped_column(String(12), index=True)
    symbol: Mapped[str] = mapped_column(String(24))
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    price: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="PREPARED", index=True)
    broker_id: Mapped[str | None] = mapped_column(String(256), nullable=True, unique=True)
    filled_quantity: Mapped[Decimal] = mapped_column(Numeric(28, 10), default=0)
    filled_amount: Mapped[Decimal] = mapped_column(Numeric(28, 10), default=0)
    costs: Mapped[Decimal] = mapped_column(Numeric(28, 10), default=0)
    costs_final: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(300), default="")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class Ledger(Base):
    __tablename__ = "ledger"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    intent_id: Mapped[str] = mapped_column(String(36), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    amount: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    costs: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AccountState(Base):
    venue: Mapped[str] = mapped_column(String(12), primary_key=True, default="toss")
    __tablename__ = "account_states"
    mode: Mapped[str] = mapped_column(String(12), primary_key=True)
    capital: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    high_water: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    daily_anchor: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    trading_day: Mapped[str] = mapped_column(String(10), default="")
    halted: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(300), default="")


class CandleRow(Base):
    venue: Mapped[str] = mapped_column(String(12), default="toss", index=True)
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("venue", "symbol", "interval", "timestamp", name="uq_candle_venue_time"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    interval: Mapped[str] = mapped_column(String(4))
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    data: Mapped[dict] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(32), default="toss")


class Command(Base):
    __tablename__ = "commands"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action: Mapped[str] = mapped_column(String(24))
    payload: Mapped[dict] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="QUEUED", index=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String(32))
    strategy_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    notify: Mapped[bool] = mapped_column(Boolean, default=False)
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class Snapshot(Base):
    venue: Mapped[str] = mapped_column(String(12), default="toss", index=True)
    __tablename__ = "snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    mode: Mapped[str] = mapped_column(String(12), index=True)
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class BacktestJob(Base):
    __tablename__ = "backtest_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    status: Mapped[str] = mapped_column(String(24), default="QUEUED", index=True)
    request: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Recommendation(Base):
    __tablename__ = "recommendations"
    __table_args__ = (UniqueConstraint("job_id", "candidate_id", name="uq_recommendation_source"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100))
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    candidate_id: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[dict] = mapped_column(JSON)
    strategy_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class RuntimeState(Base):
    __tablename__ = "runtime_state"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class DiscoveryPlan(Base):
    __tablename__ = "discovery_plans"
    venue: Mapped[str] = mapped_column(String(12), primary_key=True)
    request: Mapped[dict] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class DiscoveryRun(Base):
    __tablename__ = "discovery_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    venue: Mapped[str] = mapped_column(String(12), index=True)
    status: Mapped[str] = mapped_column(String(24), default="COLLECTING", index=True)
    request: Mapped[dict] = mapped_column(JSON)
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    strategy_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class LoginChallenge(Base):
    __tablename__ = "login_challenges"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    verifier: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
