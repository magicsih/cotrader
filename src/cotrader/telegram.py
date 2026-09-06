import asyncio
import logging
import time
from datetime import timedelta

import httpx
from sqlalchemy import select

from cotrader.db import SingleWriter
from cotrader.markets import VENUES, currency, portfolio_key
from cotrader.models import Event, RuntimeState, Strategy, now
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
            if response.status_code != 200 or not data.get("ok"):
                raise RuntimeError(f"telegram-http-{response.status_code}")
            return data["result"]
        except (httpx.HTTPError, ValueError):
            raise RuntimeError("telegram-unavailable") from None

    async def send(self, message, buttons=None):
        payload = {"chat_id": self.settings.telegram_me, "text": message[:4000]}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        receipt = await self.call("sendMessage", payload)
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
                            for command, description in (
                                ("status", "봇 상태와 운용 손익"),
                                ("account", "토스 실제 계좌 조회"),
                                ("crypto", "업비트 실제 계좌 조회"),
                                ("guide", "종목별 시작 가이드"),
                                ("strategies", "전략 확인과 승인"),
                                ("pause", "전체 주문 중단"),
                                ("web", "통계 화면 안내"),
                                ("help", "사용 방법"),
                            )
                        ],
                    },
                )
                if first_connection:
                    await self.send(
                        "Cotrader 텔레그램 연결을 확인했습니다.\n/account 실제 계좌 조회\n/status 봇 상태\n/help 사용 방법\n현재 실거래 주문은 비활성화되어 있습니다."
                    )
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
            if message.get("date", 0) < self.started_at:
                await self.call(
                    "answerCallbackQuery",
                    {
                        "callback_query_id": callback["id"],
                        "text": "/strategies로 최신 승인 버튼을 다시 열어주세요.",
                    },
                )
                return
            data = callback.get("data", "").split(":")
            if data[0] in {"start", "pause"} and len(data) >= 2:
                payload = {"strategy_id": data[1]}
                if data[0] == "start":
                    if len(data) != 4 or not data[2].isdigit():
                        return
                    payload["version"] = int(data[2])
                    payload["approval"] = data[3]
                async with self.sessions.begin() as session:
                    await enqueue(
                        session,
                        f"telegram-{update['update_id']}",
                        data[0],
                        payload,
                        f"telegram:{self.settings.telegram_me}",
                    )
                await self.call(
                    "answerCallbackQuery",
                    {
                        "callback_query_id": callback["id"],
                        "text": "요청을 접수했습니다. 처리 결과를 확인하세요.",
                    },
                )
            return
        text = message.get("text", "").split()
        command = text[0].split("@")[0] if text else ""
        if command == "/pause" and message.get("date", 0) < self.started_at:
            return
        if command in {"/start", "/help"}:
            await self.send(
                "Cotrader\n/status 상태·손익\n/account 토스 실제 계좌\n/crypto 업비트 실제 계좌\n/guide 시작 가이드\n/strategies 전략·승인\n/pause 모든 주문 중단\n/web 통계·백테스트\n\n전략 예산 안에서 자동 실행합니다. 중단 시 보유분은 매도하지 않습니다."
            )
        elif command == "/account":
            from cotrader.account import account_message, account_view

            async with self.sessions() as session:
                data = account_view(await session.get(RuntimeState, "broker_account"))
            await self.send(account_message(data))
            async with self.sessions.begin() as session:
                await put_runtime(
                    session, "telegram_account_reply", {"update_id": update["update_id"], "status": "SENT"}
                )
        elif command == "/crypto":
            from cotrader.account import account_view, crypto_account_message

            async with self.sessions() as session:
                data = account_view(await session.get(RuntimeState, "upbit_account"))
            await self.send(crypto_account_message(data))
        elif command == "/guide":
            await self.send(
                "실제 종목의 데이터 수집 → 검증 → 모의매매 순서로 따라가세요.",
                [[{"text": "사용 가이드 열기", "url": self.settings.public_url + "/guide"}]],
            )
        elif command == "/web":
            if self.settings.auth_mode == "github":
                await self.send(
                    "GitHub 본인 계정으로 로그인해 통계를 확인하세요.",
                    [[{"text": "Cotrader 열기", "url": self.settings.public_url}]],
                )
                return
            if self.settings.auth_mode != "telegram" or not self.settings.public_url.startswith("https://"):
                await self.send(
                    "웹 통계는 현재 개발 컴퓨터의 http://127.0.0.1:8000 에서 확인할 수 있습니다. 휴대폰용 화면은 HTTPS 주소와 인증 설정 후 연결됩니다."
                )
                return
            await self.send(
                "통계와 전략 설정을 엽니다.",
                [[{"text": "Cotrader 열기", "web_app": {"url": self.settings.public_url}}]],
            )
        elif command == "/status":
            async with self.sessions() as session:
                status = await session.get(RuntimeState, "engine")
                rows = [
                    await session.get(RuntimeState, portfolio_key(venue, mode))
                    for venue in VENUES
                    for mode in ("paper", "live")
                ]
            lines = ["Cotrader 상태", str(status.data if status else "실행기 연결 대기")]
            for row in rows:
                if row:
                    p = row.data
                    lines.append(
                        f"{p['venue']} {p['mode']} · 평가 {p['equity'] or '확인 불가'} {p['currency']} · 비용 {p['costs']} {p['currency']} · 미체결 {p['pending_orders']} · {'중단' if p['halted'] else '대기/운영'}"
                    )
            await self.send("\n".join(lines))
        elif command == "/strategies":
            async with self.sessions() as session:
                rows = (
                    await session.scalars(
                        select(Strategy)
                        .where(Strategy.status != "ARCHIVED")
                        .order_by(Strategy.created_at)
                        .limit(20)
                    )
                ).all()
            if not rows:
                await self.send("저장된 전략이 없습니다. /web 에서 종목·예산·가격 범위를 먼저 설정하세요.")
            for row in rows:
                c = row.config
                unit = "개" if row.venue == "upbit" else "주"
                cur = currency(row.venue)
                risk = self.settings.risk_for(row.venue)
                details = f"{row.name} · {row.symbol} · {row.mode}\n상태 {row.status}\n예산 {c['budget']} {cur} · 전략 {c['kind']}\n"
                if c["kind"] == "grid":
                    details += f"범위 {c['lower']}~{c['upper']} {cur} · {c['grids']}단계 · {c['spacing']}\n"
                    from cotrader.domain import StrategySpec, levels, slot_quantity

                    spec = StrategySpec.model_validate(c)
                    prices = levels(spec)
                    details += (
                        "\n".join(
                            f"{price} → {prices[i + 1]} {cur} · {slot_quantity(spec, price)}{unit}"
                            for i, price in enumerate(prices[:-1])
                        )
                        + "\n"
                    )
                    details += f"하락 시 신규 매수 보류: {'사용' if c['signal_gate'] else '사용 안 함'}\n"
                details += f"EMA {c['fast']}/{c['slow']} · RSI {c['rsi_period']} · 진입 {c['rsi_entry']}/매도 {c['rsi_exit']}\n수수료 가정 {c['commission_rate']} · 체결 비용 {c['slippage_bps']}bp\n"
                details += f"{c['timeframe']}분 신호 · 최대 호가 차이 {c['max_spread_bps']}bp\n손실 기준: 하루 {risk['daily_loss']} {cur}, 고점 대비 {risk['drawdown']} {cur}\n전체 세션 · 중단 시 보유 유지\n설정 버전 {row.version}"
                buttons = [
                    [
                        {
                            "text": "위 설정 승인·시작",
                            "callback_data": f"start:{row.id}:{row.version}:{approval_digest(row, self.settings)}",
                        },
                        {"text": "중단", "callback_data": f"pause:{row.id}"},
                    ]
                ]
                await self.send(details, buttons)
        elif command == "/pause":
            async with self.sessions.begin() as session:
                await enqueue(
                    session,
                    f"telegram-{update['update_id']}",
                    "pause_all",
                    {},
                    f"telegram:{self.settings.telegram_me}",
                )
            await self.send(
                "전체 중단을 접수했습니다. 미체결 주문 취소 결과는 별도로 확인하며 보유 자산은 유지합니다."
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
            await self.send(f"{event.message}\n확인번호 {event.id[:8]}")
            async with self.sessions.begin() as session:
                row = await session.get(Event, event.id)
                row.sent = True
        async with self.sessions.begin() as session:
            heartbeat = await session.get(RuntimeState, "engine")
            alert = await session.get(RuntimeState, "telegram_engine_alert")
            stale = heartbeat is None or now() - heartbeat.updated_at > timedelta(minutes=2)
            if stale and (alert is None or not alert.data.get("stale")):
                session.add(
                    Event(
                        kind="health",
                        message="주문 실행기 응답이 2분 이상 없습니다. 미체결 주문은 증권사 앱에서 확인하세요.",
                        notify=True,
                    )
                )
            await put_runtime(session, "telegram_engine_alert", {"stale": stale})
