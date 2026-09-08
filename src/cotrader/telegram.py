import asyncio
import logging
import time
from datetime import timedelta
from uuid import UUID

import httpx
from sqlalchemy import select

from cotrader import telegram_views as view
from cotrader.account import account_view
from cotrader.db import SingleWriter
from cotrader.domain import ACTIVE_ORDERS
from cotrader.markets import VENUES, portfolio_key
from cotrader.models import Command, DiscoveryRun, Event, Intent, RuntimeState, Strategy, now
from cotrader.profit import profit_summary
from cotrader.services import approval_digest, enqueue, put_runtime

LOG = logging.getLogger(__name__)


class TelegramBot:
    def __init__(self, settings, sessions, db):
        self.settings, self.sessions, self.db = settings, sessions, db
        self.started_at = int(time.time())
        self.notifications_since = now()
        # Never log this URL: Telegram puts the secret in the path.
        self.client = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{settings.telegram_token.get_secret_value()}/", timeout=40
        )

    async def call(self, method, payload):
        try:
            response = await self.client.post(method, json=payload)
            data = response.json()
            if method == "editMessageText" and str(data.get("description", "")).startswith(
                "Bad Request: message is not modified"
            ):
                return None
            if response.status_code != 200 or not data.get("ok"):
                raise RuntimeError(f"telegram-http-{response.status_code}")
            return data["result"]
        except (httpx.HTTPError, ValueError):
            raise RuntimeError("telegram-unavailable") from None

    async def send(self, blocks, buttons=None, message_id=None):
        if isinstance(blocks, str):
            blocks = [view.paragraph(blocks)]
        payload = {
            "chat_id": self.settings.telegram_me,
            "rich_message": {"blocks": blocks},
            "reply_markup": {"inline_keyboard": buttons or []},
        }
        if message_id is not None:
            await self.call("editMessageText", {**payload, "message_id": message_id})
            return
        receipt = await self.call("sendRichMessage", payload)
        async with self.sessions.begin() as session:
            await put_runtime(session, "telegram_delivery", {"message_id": receipt["message_id"]})

    async def run(self):
        try:
            async with SingleWriter(self.db, "cotrader:telegram") as lock:
                info = await self.call("getWebhookInfo", {})
                if info.get("url"):
                    await self.connection_status("ERROR", error="기존 웹훅 연결이 있어 시작하지 않았습니다")
                    return
                identity = await self.call("getMe", {})
                chat = await self.call("getChat", {"chat_id": self.settings.telegram_me})
                if chat.get("type") != "private" or chat.get("id") != self.settings.telegram_me:
                    await self.connection_status("ERROR", error="허용한 개인 대화방이 아닙니다")
                    return
                async with self.sessions.begin() as session:
                    activation = await session.get(RuntimeState, "telegram_activation")
                    first_connection = activation is None
                    if activation:
                        from datetime import datetime

                        self.notifications_since = datetime.fromisoformat(activation.data["since"])
                    else:
                        await put_runtime(
                            session, "telegram_activation", {"since": self.notifications_since.isoformat()}
                        )
                await self.call(
                    "setMyCommands",
                    {
                        "scope": {"type": "chat", "chat_id": self.settings.telegram_me},
                        "commands": [
                            {"command": command, "description": description}
                            for command, description in view.commands(self.settings.paper_lab_enabled)
                        ],
                    },
                )
                await self.call(
                    "setChatMenuButton",
                    {"chat_id": self.settings.telegram_me, "menu_button": {"type": "commands"}},
                )
                if first_connection:
                    await self.screen("menu")
                while True:
                    await lock.verify()
                    try:
                        async with self.sessions() as session:
                            offset = await session.get(RuntimeState, "telegram_offset")
                        updates = await self.call(
                            "getUpdates",
                            {
                                "offset": offset.data["offset"] if offset else 0,
                                "timeout": 10,
                                "allowed_updates": ["message", "callback_query"],
                            },
                        )
                        for update in updates:
                            await self.handle(update)
                            async with self.sessions.begin() as session:
                                await put_runtime(
                                    session, "telegram_offset", {"offset": update["update_id"] + 1}
                                )
                        await self.notifications()
                        await self.connection_status("CONNECTED", username=identity.get("username"))
                    except RuntimeError as exc:
                        await self.connection_status("ERROR", error=str(exc))
                        LOG.warning("%s", str(exc))
                        await asyncio.sleep(5)
        except RuntimeError as exc:
            await self.connection_status("ERROR", error=str(exc))
            LOG.warning("%s", str(exc))
        finally:
            await self.client.aclose()

    async def connection_status(self, status, **data):
        async with self.sessions.begin() as session:
            await put_runtime(session, "telegram", {"status": status, **data})

    async def pending_settings(self, session, strategy):
        commands = (
            await session.scalars(
                select(Command).where(
                    Command.action.in_(["set_market_cautions", "set_usdt_capital"]),
                    Command.status.in_(["QUEUED", "RUNNING"]),
                )
            )
        ).all()
        return any(
            (c.action == "set_market_cautions" and c.payload.get("strategy_id") == strategy.id)
            or (c.action == "set_usdt_capital" and strategy.venue == "upbit_usdt" and strategy.mode == "live")
            for c in commands
        )

    async def screen(self, name, page=0, message_id=None, strategy_id=None, notice=None, research_id=None):
        async with self.sessions() as session:
            if name in {"menu", "help"}:
                blocks, buttons = view.menu(self.settings.paper_lab_enabled)
                if name == "help":
                    commands = view.commands(self.settings.paper_lab_enabled)
                    blocks += [
                        view.paragraph("\n".join(f"/{cmd} · {description}" for cmd, description in commands)),
                        view.footer(
                            "모의 원장은 /paper와 /paper_data, 실제 계좌 조회는 /pocket과 /toss로 분리합니다."
                            if self.settings.paper_lab_enabled
                            else "/crypto는 /pocket, /account는 /toss와 같습니다. 새로고침은 최근 계좌 조회 값을 다시 표시합니다.\n/pause는 전체 중단을 요청하며 보유 자산은 매도하지 않습니다."
                        ),
                    ]
            elif name in {"paper", "paper_data"}:
                from datetime import UTC, datetime

                from cotrader.paper_lab import report

                data = await report(session, datetime.now(UTC))
                data["enabled"] = self.settings.paper_lab_enabled
                runtime = await session.get(RuntimeState, "paper_lab")
                blocks, buttons = getattr(view, name)(data, runtime.data if runtime else None, page)
            elif self.settings.paper_lab_enabled and name in {
                "detail",
                "orders",
                "profit",
                "status",
                "strategies",
                "paper_only",
            }:
                blocks, buttons = view.paper_only_notice()
            elif name in {"pocket", "toss"}:
                key = "upbit_account" if name == "pocket" else "broker_account"
                data = account_view(await session.get(RuntimeState, key))
                blocks, buttons = getattr(view, name)(data, page, self.settings.paper_lab_enabled)
            elif name == "profit":
                blocks, buttons = view.profit(await profit_summary(session))
            elif name == "status":
                blocks, buttons = view.status(
                    await session.get(RuntimeState, "engine"),
                    await session.get(RuntimeState, "engine:upbit"),
                    [
                        await session.get(RuntimeState, portfolio_key(venue, mode))
                        for venue in VENUES
                        for mode in ("paper", "live")
                    ],
                )
            elif name == "strategies":
                rows = (
                    await session.scalars(
                        select(Strategy)
                        .where(Strategy.status != "ARCHIVED")
                        .order_by(Strategy.created_at, Strategy.id)
                    )
                ).all()
                blocks, buttons = view.strategies(rows, page)
            elif name == "detail":
                row = await session.get(Strategy, strategy_id)
                if not row or row.status == "ARCHIVED":
                    blocks, buttons = (
                        [view.paragraph("전략이 없거나 보관되었습니다.")],
                        view.nav("strategies"),
                    )
                else:
                    blocks, buttons = view.strategy(
                        row, self.settings, await self.pending_settings(session, row), notice
                    )
            elif name == "orders":
                rows = (
                    await session.scalars(
                        select(Intent)
                        .where(Intent.mode == "live", Intent.status.in_(ACTIVE_ORDERS))
                        .order_by(Intent.created_at.desc(), Intent.id)
                    )
                ).all()
                blocks, buttons = view.orders(rows, page)
            elif name == "research":
                row = (
                    await session.get(DiscoveryRun, research_id)
                    if research_id
                    else await session.scalar(
                        select(DiscoveryRun)
                        .where(DiscoveryRun.venue == "toss")
                        .order_by(DiscoveryRun.created_at.desc())
                        .limit(1)
                    )
                )
                blocks, buttons = view.research(row, self.settings.public_url)
            elif name in {"web", "guide"}:
                if not self.settings.public_url.startswith("https://"):
                    blocks, buttons = (
                        [view.paragraph("휴대폰용 웹 화면은 HTTPS 주소와 인증 설정 후 연결됩니다.")],
                        view.nav("menu"),
                    )
                else:
                    url = self.settings.public_url + ("/guide" if name == "guide" else "")
                    control = (
                        {"web_app": {"url": url}} if self.settings.auth_mode == "telegram" else {"url": url}
                    )
                    blocks = [
                        view.heading("Cotrader 웹 화면"),
                        view.paragraph("연결된 본인 계정으로 로그인해 상세 설정과 통계를 확인하세요."),
                    ]
                    buttons = [
                        [{"text": "가이드 열기" if name == "guide" else "Cotrader 열기", **control}],
                        *view.nav("menu"),
                    ]
            else:
                blocks, buttons = view.menu(self.settings.paper_lab_enabled)
                blocks.insert(1, view.paragraph("알 수 없는 명령입니다. 아래 메뉴를 선택하세요."))
        await self.send(blocks, buttons, message_id=message_id)

    async def handle(self, update):
        callback = update.get("callback_query")
        message = callback.get("message", {}) if callback else update.get("message", {})
        sender = callback.get("from", {}) if callback else message.get("from", {})
        if (
            sender.get("id") != self.settings.telegram_me
            or message.get("chat", {}).get("id") != self.settings.telegram_me
            or message.get("chat", {}).get("type") != "private"
        ):
            return
        if callback:
            parts = callback.get("data", "").split(":")
            # Read-only navigation remains usable after a restart; approvals do not.
            await self.call("answerCallbackQuery", {"callback_query_id": callback["id"]})
            if self.settings.paper_lab_enabled and (
                (
                    len(parts) == 3
                    and parts[0] == "nav"
                    and parts[1] in {"orders", "profit", "status", "strategies"}
                )
                or parts[0] in {"detail", "run", "start", "pause"}
            ):
                await self.screen("paper_only", message_id=message.get("message_id"))
                return
            if len(parts) == 3 and parts[0] == "nav" and parts[2].isdigit() and len(parts[2]) <= 6:
                await self.screen(parts[1], int(parts[2]), message.get("message_id"))
                return
            if parts[0] in {"detail", "research"} and len(parts) == 2:
                try:
                    strategy_id = str(UUID(parts[1]))
                except ValueError:
                    return
                await self.screen(
                    parts[0],
                    message_id=message.get("message_id"),
                    **{("strategy_id" if parts[0] == "detail" else "research_id"): strategy_id},
                )
                return
            if parts[0] not in {"run", "start", "pause"} or len(parts) < 2:
                return
            try:
                strategy_id = str(UUID(parts[1]))
                payload = {"strategy_id": strategy_id}
                if parts[0] != "pause":
                    if len(parts) != 4:
                        return
                    payload.update(version=int(parts[2], 16 if parts[0] == "run" else 10), approval=parts[3])
                elif len(parts) != 2:
                    return
            except ValueError:
                return
            stale = message.get("edit_date", message.get("date", 0)) < self.started_at
            notice = "이전 확인 화면입니다. 최신 설정을 다시 확인한 후 요청하세요."
            async with self.sessions.begin() as session:
                row = await session.get(Strategy, strategy_id, with_for_update=True)
                if not row or row.status == "ARCHIVED":
                    stale = True
                elif parts[0] != "pause":
                    stale = (
                        stale
                        or payload["version"] != row.version
                        or payload["approval"] != approval_digest(row, self.settings)
                        or await self.pending_settings(session, row)
                    )
                if not stale:
                    await enqueue(
                        session,
                        f"telegram-{update['update_id']}",
                        "pause" if parts[0] == "pause" else "start",
                        payload,
                        f"telegram:{self.settings.telegram_me}",
                    )
                    notice = "요청을 접수했습니다. 처리 결과는 알림과 새로고침으로 확인하세요."
            await self.screen(
                "detail", message_id=message.get("message_id"), strategy_id=strategy_id, notice=notice
            )
            return
        tokens = message.get("text", "").split()
        command = tokens[0].split("@")[0] if tokens else ""
        if command == "/pause":
            if self.settings.paper_lab_enabled:
                await self.screen("paper_only")
                return
            if message.get("date", 0) < self.started_at:
                return
            async with self.sessions.begin() as session:
                await enqueue(
                    session,
                    f"telegram-{update['update_id']}",
                    "pause_all",
                    {},
                    f"telegram:{self.settings.telegram_me}",
                )
            await self.send(
                [
                    view.heading("전체 중단 요청 접수"),
                    view.paragraph("미체결 주문 취소 결과를 확인 중입니다. 보유 자산은 유지합니다."),
                ],
                [[view.button("주문 상태 확인", "nav:orders:0")]],
            )
            return
        name = {"/start": "menu", "/account": "toss", "/crypto": "pocket"}.get(
            command, command.removeprefix("/")
        )
        await self.screen(name)
        if name == "toss":
            async with self.sessions.begin() as session:
                await put_runtime(
                    session, "telegram_account_reply", {"update_id": update["update_id"], "status": "SENT"}
                )

    async def notifications(self):
        async with self.sessions() as session:
            events = (
                await session.scalars(
                    select(Event)
                    .where(
                        Event.notify.is_(True),
                        Event.sent.is_(False),
                        Event.created_at >= self.notifications_since,
                    )
                    .order_by(Event.created_at)
                    .limit(10)
                )
            ).all()
        for event in events:
            # Delivery is at-least-once; include a stable receipt to recognize retries.
            buttons = (
                [
                    [
                        view.button("모의 비교 운용", "nav:paper:0"),
                        view.button("모의 데이터", "nav:paper_data:0"),
                    ]
                ]
                if self.settings.paper_lab_enabled
                else [[view.button("운영 상태", "nav:status:0"), view.button("미체결 주문", "nav:orders:0")]]
            )
            if event.strategy_id and not self.settings.paper_lab_enabled:
                buttons.insert(0, [view.button("해당 전략 보기", f"detail:{event.strategy_id}")])
            elif event.data.get("discovery_id"):
                buttons.insert(0, [view.button("발굴 결과 보기", f"research:{event.data['discovery_id']}")])
            async with self.sessions() as session:
                intent = (
                    await session.get(Intent, event.data["intent_id"])
                    if event.kind == "fill" and event.data.get("intent_id")
                    else None
                )
            await self.send(view.notification(event, intent), buttons)
            async with self.sessions.begin() as session:
                row = await session.get(Event, event.id)
                row.sent = True
        async with self.sessions.begin() as session:
            runtime_key = "paper_lab" if self.settings.paper_lab_enabled else "engine"
            alert_key = (
                "telegram_paper_lab_alert" if self.settings.paper_lab_enabled else "telegram_engine_alert"
            )
            heartbeat = await session.get(RuntimeState, runtime_key)
            alert = await session.get(RuntimeState, alert_key)
            stale = heartbeat is None or now() - heartbeat.updated_at > timedelta(minutes=2)
            if stale and (alert is None or not alert.data.get("stale")):
                session.add(
                    Event(
                        kind="health",
                        message=(
                            "모의 실행기 갱신이 2분 이상 없습니다. /paper의 마지막 저장 값을 현재값으로 보지 마세요."
                            if self.settings.paper_lab_enabled
                            else "주문 실행기 응답이 2분 이상 없습니다. 미체결 주문은 업비트·토스증권 앱에서 확인하세요."
                        ),
                        notify=True,
                    )
                )
            await put_runtime(session, alert_key, {"stale": stale})
