import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import func, select

from cotrader import telegram_views as view
from cotrader.config import Settings
from cotrader.domain import StrategySpec
from cotrader.models import Command, Event, PaperPortfolio, RuntimeState
from cotrader.paper_lab import bootstrap_lab
from cotrader.services import approval_digest, create_strategy, enqueue
from cotrader.telegram import TelegramBot
from cotrader.upbit import UpbitBroker


def settings():
    return Settings(TELEGRAM_API_KEY="fixture-token", TELEGRAM_ME=7)


def paper_settings():
    return Settings(
        TELEGRAM_API_KEY="fixture-token",
        TELEGRAM_ME=7,
        paper_lab_enabled=True,
        paper_lab_usd="5000",
        paper_lab_usdt="500",
        market_source="toss",
        upbit_enabled=True,
    )


def update(data, *, date=None, edited=None, sender=7):
    message = {"message_id": 42, "date": date or int(time.time()), "chat": {"id": 7, "type": "private"}}
    if edited:
        message["edit_date"] = edited
    return {
        "update_id": 100,
        "callback_query": {
            "id": "fixture-callback",
            "from": {"id": sender},
            "message": message,
            "data": data,
        },
    }


async def setup(db):
    database, sessions = db
    bot = TelegramBot(settings(), sessions, database)
    bot.send = AsyncMock()
    bot.call = AsyncMock()
    async with sessions.begin() as session:
        row = await create_strategy(
            session, "검증 <설정> & 보유", StrategySpec(symbol="SPY", kind="trend", budget="1000"), "paper"
        )
    return bot, row


async def setup_paper(db):
    database, sessions = db
    config = paper_settings()
    bot = TelegramBot(config, sessions, database)
    bot.send = AsyncMock()
    bot.call = AsyncMock()
    at = datetime.now(UTC)
    async with sessions.begin() as session:
        await bootstrap_lab(session, config, at)
        await session.flush()
        row = await session.get(PaperPortfolio, "toss-base-v1")
        row.state = {
            **row.state,
            "action": "WAIT",
            "reason": "미국 정규장 시작 대기",
            "signal_date": "2026-09-05",
            "nav_at": at.isoformat(),
            "next_check_at": (at + timedelta(hours=1)).isoformat(),
            "history_fingerprint": "a" * 64,
            "conditions": {
                "SPY": {
                    "value": "650",
                    "average": "625",
                    "enter_pass": True,
                    "exit_pass": False,
                    "target": "0.142857",
                }
            },
        }
        session.add(RuntimeState(key="paper_lab", data={"enabled": True, "checked_at": at.isoformat()}))
    return bot


def start_button(row, bot):
    _, buttons = view.strategy(row, bot.settings)
    return next(
        b["callback_data"] for r in buttons for b in r if b.get("callback_data", "").startswith("run:")
    )


def text_update(text, update_id=100):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 42,
            "date": int(time.time()),
            "from": {"id": 7},
            "chat": {"id": 7, "type": "private"},
            "text": text,
        },
    }


def test_paper_profile_commands_remove_ambiguous_live_controls():
    names = {name for name, _ in view.commands(True)}
    assert {"paper", "paper_data", "pocket", "toss"} <= names
    assert names.isdisjoint({"orders", "profit", "strategies", "status", "pause"})
    assert {name for name, _ in view.commands(False)} == {name for name, _ in view.LIVE_COMMANDS}


async def test_paper_profile_exposes_separate_summary_and_signal_evidence(db):
    bot = await setup_paper(db)
    await bot.handle(text_update("/paper"))
    rendered = str(bot.send.call_args.args[0])
    assert "모의 비교 운용 · 실제 주문 없음" in rendered
    assert "기준 추세" in rendered and "미국 ETF" in rendered
    assert "실제 계좌 잔고·실거래 원장" not in rendered

    await bot.handle(text_update("/paper_data", 101))
    rendered = str(bot.send.call_args.args[0])
    assert "paper_portfolios와 paper_events만 읽습니다" in rendered
    assert "신호값" in rendered and "650" in rendered and "625" in rendered
    assert "진입 통과" in rendered and "이탈 미통과" in rendered
    assert "미국 정규장 시작 대기" in rendered and "다음 평가" in rendered

    await bot.handle(text_update("/pocket", 102))
    rendered = str(bot.send.call_args.args[0])
    buttons = str(bot.send.call_args.args[1])
    assert "실제 계좌의 마지막 조회 값입니다. 모의 원장과 합산하지 않습니다" in rendered
    assert "미체결 주문" not in buttons and "전략 관리" not in buttons
    await bot.client.aclose()


async def test_paper_profile_rejects_old_live_commands_and_callbacks_without_database_writes(db):
    bot = await setup_paper(db)
    for index, command in enumerate(("/profit", "/strategies", "/orders", "/status", "/pause"), 1):
        await bot.handle(text_update(command, index))
        assert "실거래 전략·주문·중단 명령은 접수하지 않았습니다" in str(bot.send.call_args.args[0])
    await bot.handle(update("run:00000000000000000000000000000000:1:stale"))
    assert "실거래 전략·주문·중단 명령은 접수하지 않았습니다" in str(bot.send.call_args.args[0])
    async with bot.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Command)) == 0
    await bot.client.aclose()


async def test_paper_profile_health_alert_uses_paper_runtime(db):
    bot = await setup_paper(db)
    async with bot.sessions.begin() as session:
        runtime = await session.get(RuntimeState, "paper_lab")
        runtime.updated_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=3)
    await bot.notifications()
    async with bot.sessions() as session:
        event = await session.scalar(select(Event).where(Event.kind == "health"))
        alert = await session.get(RuntimeState, "telegram_paper_lab_alert")
    assert "모의 실행기 갱신" in event.message
    assert alert.data == {"stale": True}
    await bot.client.aclose()


async def test_rich_account_uses_engine_pocket_snapshot_and_no_financial_commands(db):
    bot, _ = await setup(db)
    snapshot = {
        "account_label": "코트레이더 포켓",
        "checked_at": datetime.now(UTC).isoformat(),
        "balances": [
            {"currency": "BTC", "balance": "0.09", "locked": "0.01"},
            {"currency": "USDT", "balance": "1234.56789012", "locked": "0"},
            {"currency": "APENFT", "balance": "0.00000001", "locked": "0"},
        ],
    }
    async with bot.sessions.begin() as session:
        session.add(RuntimeState(key="upbit_account", data={"status": "CONNECTED", "snapshot": snapshot}))
        session.add(RuntimeState(key="broker_account", data={"status": "ERROR", "snapshot": None}))
    for name in ("/pocket", "/crypto"):
        await bot.handle(
            {
                "update_id": 1,
                "message": {"from": {"id": 7}, "chat": {"id": 7, "type": "private"}, "text": name},
            }
        )
        blocks = bot.send.call_args.args[0]
        rendered = str(blocks)
        assert "코트레이더 포켓" in rendered and "1,234.56789012" in rendered and "0.00000001" in rendered
        cells = next(b["cells"] for b in blocks if b["type"] == "table")
        assert [c["text"] for c in cells[1]] == ["BTC", "0.09", "0.01", "0.1"]
    async with bot.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Command)) == 0
    await bot.client.aclose()


def test_unknown_account_does_not_invent_pocket_identity_or_zero_balance():
    for snapshot in (None, {"checked_at": datetime.now(UTC).isoformat()}):
        blocks, _ = view.pocket({"status": "ERROR", "snapshot": snapshot, "stale": True})
        assert "코트레이더 포켓" not in str(blocks)
        assert "마지막 조회 값" in str(blocks)
        assert not any(b["type"] == "table" for b in blocks)


async def test_pocket_label_is_attached_to_actual_account_response():
    broker = UpbitBroker(Settings(upbit_account_label="코트레이더 포켓"))
    broker.request = AsyncMock(
        return_value=[
            {"currency": "BTC", "balance": "0.1", "locked": "0", "avg_buy_price": "1", "unit_currency": "KRW"}
        ]
    )
    result = await broker.account_snapshot()
    assert result["account_label"] == "코트레이더 포켓"
    broker.request.assert_awaited_once_with("GET", "/v1/accounts", private=True)
    await broker.close()


async def test_old_navigation_edits_one_message_but_never_starts_a_strategy(db):
    bot, _ = await setup(db)
    await bot.handle(update("nav:pocket:999", date=1))
    assert bot.send.call_args.kwargs["message_id"] == 42
    bot.call.assert_awaited_once_with("answerCallbackQuery", {"callback_query_id": "fixture-callback"})
    async with bot.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Command)) == 0
    await bot.client.aclose()


@pytest.mark.parametrize("change", ["version", "digest", "pending", "old_message", "foreign_user"])
async def test_outdated_or_unauthorized_start_never_enqueues(db, change):
    bot, row = await setup(db)
    data = start_button(row, bot)
    async with bot.sessions.begin() as s:
        if change == "version":
            current = await s.get(type(row), row.id)
            current.version += 1
        elif change == "pending":
            await enqueue(s, "fixture-pending", "set_market_cautions", {"strategy_id": row.id}, "fixture")
    if change == "digest":
        data = data.rsplit(":", 1)[0] + ":stale"
    await bot.handle(
        update(data, date=1 if change == "old_message" else None, sender=8 if change == "foreign_user" else 7)
    )
    async with bot.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Command).where(Command.action == "start")) == 0
    if change == "pending":
        assert "설정 변경 처리 중" in str(bot.send.call_args.args[0])
        assert not any(
            b.get("callback_data", "").startswith("run:") for r in bot.send.call_args.args[1] for b in r
        )
    await bot.client.aclose()


async def test_refreshed_message_accepts_current_approval_once_even_after_restart(db):
    bot, row = await setup(db)
    data = start_button(row, bot)
    request = update(data, date=1, edited=int(time.time()))
    await bot.handle(request)
    await bot.handle(request)
    async with bot.sessions() as s:
        commands = (await s.scalars(select(Command))).all()
        assert len(commands) == 1
        assert commands[0].status == "QUEUED"
        assert commands[0].payload == {
            "strategy_id": row.id,
            "version": row.version,
            "approval": approval_digest(row, bot.settings),
        }
    await bot.client.aclose()


async def test_start_callback_fits_telegram_limit_and_html_is_plain_data(db):
    bot, row = await setup(db)
    row.version = 2147483647
    assert len(start_button(row, bot).encode()) <= 64
    blocks, _ = view.strategy(row, bot.settings)
    assert "<설정> & 보유" in str(blocks)
    assert all("parse_mode" not in b for b in blocks)
    await bot.client.aclose()


async def test_send_and_edit_use_native_rich_tables_and_keep_long_content(db):
    bot, _ = await setup(db)
    bot.send = TelegramBot.send.__get__(bot)
    bot.call.return_value = {"message_id": 9}
    blocks = [
        view.heading("잔고"),
        view.table(["자산", "수량"], [["BTC", "0.1"]]),
        view.paragraph("가" * 4100),
    ]
    await bot.send(blocks, view.nav("pocket"))
    method, payload = bot.call.call_args.args
    assert method == "sendRichMessage" and payload["rich_message"]["blocks"] == blocks
    await bot.send(blocks, message_id=9)
    method, payload = bot.call.call_args.args
    assert method == "editMessageText" and payload["message_id"] == 9
    assert payload["reply_markup"] == {"inline_keyboard": []}
    await bot.client.aclose()


async def test_unchanged_edit_is_success_and_transport_errors_do_not_expose_token(db):
    database, sessions = db
    bot = TelegramBot(settings(), sessions, database)
    await bot.client.aclose()
    bot.client = httpx.AsyncClient(
        base_url="https://api.telegram.org/botfixture-token/",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                400, json={"ok": False, "description": "Bad Request: message is not modified"}
            )
        ),
    )
    assert await bot.call("editMessageText", {}) is None
    with pytest.raises(RuntimeError, match="telegram-http-400") as error:
        await bot.call("sendRichMessage", {})
    assert "fixture-token" not in str(error.value)
    await bot.client.aclose()


def test_pagination_and_zero_equity_are_displayed_without_truncating_or_claiming_missing():
    rows = [{"currency": f"COIN{i}", "balance": "0", "locked": "0"} for i in range(15)]
    blocks, buttons = view.pocket({"status": "CONNECTED", "snapshot": {"balances": rows}}, 2)
    assert len(next(b["cells"] for b in blocks if b["type"] == "table")) == 4
    assert any(b["callback_data"] == "nav:pocket:1" for r in buttons for b in r)
    p = SimpleNamespace(
        data={
            "venue": "toss",
            "mode": "paper",
            "currency": "USD",
            "equity": "0",
            "capital": "100",
            "costs": "1",
        }
    )
    blocks, _ = view.status(None, None, [p])
    cells = next(b["cells"] for b in blocks if b["type"] == "table")
    assert cells[1][1]["text"] == "0" and cells[2][1]["text"] == "-100"


def test_research_rejection_remains_visible_and_never_offers_start():
    row = SimpleNamespace(
        id="fixture",
        status="SUCCEEDED",
        updated_at=datetime.now(UTC) - timedelta(minutes=1),
        request={"budget": "5000", "symbols": ["SPY", "QQQ"]},
        progress={},
        result={
            "recommendation_ready": False,
            "recommendation_reasons": ["실제 관측일이 20일 미만"],
            "selected": {
                "spec": {"symbol": "SPY", "kind": "rebound"},
                "final": {"profit": "0", "max_drawdown": "0", "completed_cycles": 0},
            },
        },
    )
    blocks, buttons = view.research(row, "https://example.com")
    assert "추천 보류" in str(blocks) and "20일 미만" in str(blocks)
    assert "run:" not in json.dumps(buttons)


async def test_research_notification_opens_its_actual_venue_and_fill_uses_units(db):
    from cotrader.models import DiscoveryRun, Event, Intent

    bot, _ = await setup(db)
    async with bot.sessions.begin() as s:
        row = DiscoveryRun(
            venue="upbit_usdt",
            status="FAILED",
            request={"venue": "upbit_usdt", "budget": "500", "symbols": ["USDT-BTC"]},
            progress={},
            result={"message": "시세 부족"},
        )
        s.add(row)
        await s.flush()
    await bot.handle(update(f"research:{row.id}"))
    assert "업비트 USDT" in str(bot.send.call_args.args[0])
    assert "nav:research:0" not in str(bot.send.call_args.args[1])
    event = Event(
        id="receipt",
        kind="fill",
        message="old",
        created_at=datetime.now(UTC),
        data={"quantity": "0.0010000000", "amount": "79.50", "costs": "0.19875"},
    )
    intent = Intent(venue="upbit_usdt", symbol="USDT-BTC", side="SELL", mode="live")
    rendered = str(view.notification(event, intent))
    assert "매도" in rendered and "79.5 USDT" in rendered and "0.19875 USDT" in rendered
    assert "0.0010000000" not in rendered
    await bot.client.aclose()
