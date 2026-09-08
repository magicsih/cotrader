"""Operator-run pocket migration; never used by the trading engine."""

import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import shlex
import stat
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from cotrader.config import Settings
from cotrader.db import SingleWriter, database
from cotrader.domain import ACTIVE_ORDERS
from cotrader.markets import UPBIT_VENUES
from cotrader.models import Command, Intent, Strategy
from cotrader.upbit import account_jwt

TRANSFER = "/v1/pockets/universal_transfers"
PREFIXES = {
    "main": "UPBIT_OPEN_API_",
    "admin": "UPBIT_OPEN_API_POCKET_ADMIN_",
    "target": "UPBIT_OPEN_API_COTRADER_",
}


class MigrationError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise MigrationError(message)


def number(value):
    require(isinstance(value, str), "수량은 문자열이어야 합니다")
    result = Decimal(value)
    require(result.is_finite() and result >= 0, "수량은 유한한 음이 아닌 값이어야 합니다")
    return result


def decimal_text(value):
    return format(value, "f")


def serialize(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2)


def digest(value):
    return hashlib.sha256(serialize(value).encode()).hexdigest()


def private_read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid(), "본인 소유 일반 파일이 필요합니다")
        require(info.st_mode & 0o077 == 0, "파일 권한을 600으로 제한하세요")
        return stream.read()


def sync_directory(path):
    fd = os.open(Path(path).parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_write(path, content):
    # No overwrite, including symlinks. Keys and balances never enter Git by default.
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path)


def env_values(path):
    values = {}
    for line in private_read(path).splitlines():
        parts = shlex.split(line, comments=True)
        if parts and parts[0] == "export":
            parts = parts[1:]
        if not parts:
            continue
        require(len(parts) == 1 and "=" in parts[0], "env 파일은 KEY=value 형식이어야 합니다")
        key, value = parts[0].split("=", 1)
        require(key not in values, "env 파일에 중복 이름이 있습니다")
        values[key] = value
    return values


class PocketClient:
    def __init__(self, values, *, execute=False, client=None):
        self.keys = {}
        for role, prefix in PREFIXES.items():
            pair = (values.get(prefix + "ACCESS_KEY"), values.get(prefix + "SECRET_KEY"))
            require(all(pair), f"{role} 역할 키가 없습니다")
            self.keys[role] = pair
        require(len({pair[0] for pair in self.keys.values()}) == 3, "세 역할의 키는 서로 달라야 합니다")
        self.execute = execute
        self.client = client or httpx.AsyncClient(
            base_url="https://api.upbit.com", timeout=15, trust_env=False, follow_redirects=False
        )
        require(str(self.client.base_url) == "https://api.upbit.com", "업비트 공식 API만 사용합니다")

    async def close(self):
        await self.client.aclose()

    async def request(self, role, path, *, params=None, body=None):
        allowed = {
            "admin": {"/v1/pockets", "/v1/pockets/api_keys", TRANSFER},
            "main": {"/v1/accounts", "/v1/orders/open"},
            "target": {"/v1/accounts", "/v1/orders/open"},
        }
        require(path in allowed.get(role, set()), "허용하지 않은 API 경로입니다")
        method = "GET" if body is None else "POST"
        require(method == "GET" or (self.execute and role == "admin" and path == TRANSFER), "조회 전용입니다")
        token = account_jwt(*self.keys[role], body if body is not None else params)
        await asyncio.sleep(0.2)
        try:
            response = await self.client.request(
                method, path, params=params, json=body, headers={"Authorization": "Bearer " + token}
            )
            if response.status_code in {418, 429}:
                raise MigrationError(
                    "업비트 요청 제한: 즉시 중단합니다. 60초 이상 기다리고 같은 기록으로 확인하세요"
                )
            if not response.is_success:
                # Only a validated provider error name; never bodies or request headers.
                try:
                    code = response.json().get("error", {}).get("name", "request_failed")
                except (ValueError, AttributeError):
                    code = "request_failed"
                if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,80}", code):
                    code = "request_failed"
                if any(value in code for pair in self.keys.values() for value in pair):
                    code = "request_failed"
                raise MigrationError(f"업비트 HTTP {response.status_code} {code}: 이전 내역을 확인하세요")
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise MigrationError(
                "업비트 응답 미확인: 같은 실행 기록으로 조회하세요. 자동 재전송하지 않습니다"
            ) from None

    async def identity(self):
        pockets = await self.request("admin", "/v1/pockets")
        keys = await self.request("admin", "/v1/pockets/api_keys")
        require(isinstance(pockets, list) and isinstance(keys, list), "포켓 목록 응답 형식 오류")
        identity = {}
        permissions = {
            "admin": {"manage_pockets"},
            "main": {"view_account", "view_orders"},
            "target": {"view_account", "view_orders", "make_orders"},
        }
        for role, (access, _) in self.keys.items():
            matches = [(p["uuid"], k) for p in keys for k in p["keys"] if k.get("access_key") == access]
            require(len(matches) == 1, f"{role} 키의 포켓을 단일하게 확인할 수 없습니다")
            pocket_id, key = matches[0]
            require(permissions[role] <= set(key["permissions"]), f"{role} 키 권한이 부족합니다")
            match = [p for p in pockets if p["uuid"] == pocket_id]
            require(len(match) == 1, "포켓 정보가 일치하지 않습니다")
            pocket = match[0]
            require(str(UUID(pocket_id)) == pocket_id, "포켓 UUID 형식 오류")
            require(
                pocket["type"] == ("user_spot_trading" if role == "target" else "main"),
                "키의 포켓 종류가 다릅니다",
            )
            identity[role] = {"uuid": pocket_id, "name": pocket["name"]}
        require(identity["main"] == identity["admin"], "관리 키와 메인 키의 포켓이 다릅니다")
        require(identity["main"]["uuid"] != identity["target"]["uuid"], "출발·도착 포켓이 같습니다")
        return identity

    async def snapshot(self):
        result = {"identity": await self.identity(), "balances": {}, "orders": {}}
        for role in ("main", "target"):
            rows = await self.request(role, "/v1/accounts")
            require(isinstance(rows, list), "잔고 목록 응답 형식 오류")
            balances = {}
            for row in rows:
                code = row["currency"]
                require(
                    re.fullmatch(r"[A-Z0-9]{1,20}", code) and code not in balances,
                    "자산 코드 중복 또는 형식 오류",
                )
                balances[code] = {field: decimal_text(number(row[field])) for field in ("balance", "locked")}
            result["balances"][role] = balances
            orders, seen = [], set()
            for page in range(1, 101):
                batch = await self.request(
                    role,
                    "/v1/orders/open",
                    params={
                        "states[]": ["wait", "watch"],
                        "page": page,
                        "limit": 100,
                        "order_by": "asc",
                    },
                )
                require(isinstance(batch, list), "주문 목록 응답 형식 오류")
                for row in batch:
                    require(row["uuid"] not in seen, "조회 도중 주문 목록이 변했습니다")
                    seen.add(row["uuid"])
                    orders.append(
                        {
                            k: row.get(k)
                            for k in (
                                "uuid",
                                "identifier",
                                "market",
                                "side",
                                "price",
                                "remaining_volume",
                                "state",
                            )
                        }
                    )
                if len(batch) < 100:
                    break
            else:
                raise MigrationError("주문 조회 페이지 상한을 초과했습니다")
            result["orders"][role] = orders
        return result

    async def receipt(self, plan, item):
        start = datetime.fromisoformat(plan["created_at"])
        end = min(datetime.now(UTC), start + timedelta(days=7))
        rows = await self.request(
            "admin",
            TRANSFER,
            params={
                "identifiers[]": [item["identifier"]],
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "limit": 100,
            },
        )
        require(isinstance(rows, list) and len(rows) <= 1, "이전 내역 조회 응답이 모호합니다")
        if not rows:
            return None
        row = rows[0]
        expected = transfer_body(plan, item)
        require(
            all(row.get(k) == expected[k] for k in ("from", "to", "currency", "identifier")),
            "이전 내역 대상 불일치",
        )
        require(number(row["amount"]) == number(item["amount"]), "이전 내역 수량 불일치")
        require(
            row.get("uuid") and row.get("state") in {"submitted", "processing", "done", "failed"},
            "이전 상태 응답 오류",
        )
        return {k: row[k] for k in ("uuid", "state")}


def no_pending(snapshot):
    require(not any(snapshot["orders"].values()), "양쪽 포켓의 미체결·예약 주문을 먼저 종료하고 대조하세요")
    require(
        all(number(b["locked"]) == 0 for rows in snapshot["balances"].values() for b in rows.values()),
        "주문·기타 잠긴 잔고가 남아 있습니다",
    )


def totals(snapshot, role):
    return {k: number(v["balance"]) + number(v["locked"]) for k, v in snapshot["balances"][role].items()}


async def ledger_evidence(sessions):
    async with sessions() as session:
        strategies = (
            await session.scalars(
                select(Strategy)
                .where(Strategy.mode == "live", Strategy.venue.in_(UPBIT_VENUES))
                .order_by(Strategy.id)
            )
        ).all()
        require(not any(s.status == "RUNNING" for s in strategies), "기존 업비트 전략을 먼저 중단하세요")
        pending = await session.scalar(
            select(Intent.id)
            .where(Intent.mode == "live", Intent.venue.in_(UPBIT_VENUES), Intent.status.in_(ACTIVE_ORDERS))
            .limit(1)
        )
        require(pending is None, "기존 장부에 미체결·미확인·취소 확인 중 주문이 있습니다")
        commands = await session.scalar(
            select(Command.id).where(Command.status.in_(["QUEUED", "RUNNING"])).limit(1)
        )
        require(commands is None, "처리 중 명령이 있습니다. 명령 처리를 마친 뒤 엔진을 중지하세요")
        return [
            {
                "id": s.id,
                "symbol": s.symbol,
                "status": s.status,
                "version": s.version,
                "config": s.config,
                "state": s.state,
            }
            for s in strategies
        ]


def quantities_covered(ledger, snapshot, reserve_btc):
    moving = totals(snapshot, "main")
    moving["BTC"] = moving.get("BTC", Decimal(0)) - reserve_btc
    assigned = {}
    for strategy in ledger:
        if strategy["state"].get("funded"):
            code = strategy["symbol"].split("-", 1)[1]
            assigned[code] = assigned.get(code, Decimal(0)) + number(strategy["state"]["quantity"])
    require(
        all(moving.get(code, Decimal(0)) == quantity for code, quantity in assigned.items()),
        "보존할 BTC를 제외한 이전 수량이 봇 장부 보유량과 다릅니다",
    )


def make_plan(snapshot, ledger, db_identity, reserve_btc):
    reserve_btc = number(reserve_btc)
    no_pending(snapshot)
    require(
        totals(snapshot, "main").get("BTC", Decimal(0)) >= reserve_btc, "메인 잔고가 보존할 BTC보다 적습니다"
    )
    require(
        not any(totals(snapshot, "target").values()), "첫 이전 계획은 비어 있는 코트레이더 포켓만 허용합니다"
    )
    quantities_covered(ledger, snapshot, reserve_btc)
    plan_id = str(uuid4())
    items = []
    for code, total in sorted(totals(snapshot, "main").items()):
        quantity = total - (reserve_btc if code == "BTC" else Decimal(0))
        if quantity > 0:
            items.append(
                {"currency": code, "amount": decimal_text(quantity), "identifier": f"ct-{plan_id}-{code}"}
            )
    require(items, "이전할 잔고가 없습니다")
    return {
        "schema": 1,
        "id": plan_id,
        "created_at": datetime.now(UTC).isoformat(),
        "reserve_btc": decimal_text(reserve_btc),
        "snapshot": snapshot,
        "ledger": ledger,
        "database": db_identity,
        "items": items,
    }


def make_return_plan(snapshot, ledger, db_identity, excluded=()):
    """Return the operator pocket in kind, preserving the main pocket and exclusions."""
    no_pending(snapshot)
    excluded = sorted(set(excluded))
    require(all(re.fullmatch(r"[A-Z0-9]{1,20}", code) for code in excluded), "제외 자산 코드 오류")
    available = totals(snapshot, "target")
    assigned = {}
    for strategy in ledger:
        require(strategy["status"] != "RUNNING", "반환 전 전략을 중단하세요")
        if strategy["state"].get("funded"):
            code = strategy["symbol"].split("-", 1)[1]
            assigned[code] = assigned.get(code, Decimal(0)) + number(strategy["state"]["quantity"])
    require(
        all(available.get(code, Decimal(0)) >= quantity for code, quantity in assigned.items()),
        "반환 포켓의 실제 수량이 전략 장부보다 적습니다",
    )
    require(
        not any(assigned.get(code, Decimal(0)) for code in excluded),
        "운용 보유 자산을 반환에서 제외할 수 없습니다",
    )
    plan_id = str(uuid4())
    items = [
        {"currency": code, "amount": decimal_text(quantity), "identifier": f"ct-{plan_id}-{code}"}
        for code, quantity in sorted(available.items())
        if quantity > 0 and code not in excluded
    ]
    require(items, "반환할 잔고가 없습니다")
    return {
        "schema": 2,
        "direction": "to-main",
        "excluded": excluded,
        "id": plan_id,
        "created_at": datetime.now(UTC).isoformat(),
        "reserve_btc": decimal_text(totals(snapshot, "main").get("BTC", Decimal(0))),
        "snapshot": snapshot,
        "ledger": ledger,
        "database": db_identity,
        "items": items,
    }


def validate_plan(plan):
    require(plan["schema"] in {1, 2}, "지원하지 않는 이전 계획입니다")
    require(str(UUID(plan["id"])) == plan["id"], "계획 ID 형식 오류")
    created = datetime.fromisoformat(plan["created_at"])
    require(created.tzinfo is not None and created <= datetime.now(UTC), "계획 생성 시각 오류")
    if plan["schema"] == 2:
        require(plan.get("direction") == "to-main", "반환 계획의 방향이 다릅니다")
        regenerated = make_return_plan(plan["snapshot"], plan["ledger"], plan["database"], plan["excluded"])
        require(plan["reserve_btc"] == regenerated["reserve_btc"], "메인 BTC 원본 잔고가 다릅니다")
    else:
        regenerated = make_plan(plan["snapshot"], plan["ledger"], plan["database"], plan["reserve_btc"])
    expected = [
        {**item, "identifier": f"ct-{plan['id']}-{item['currency']}"} for item in regenerated["items"]
    ]
    require(plan["items"] == expected, "계획의 자산·수량·식별자가 원본 잔고와 일치하지 않습니다")


def transfer_body(plan, item):
    identity = plan["snapshot"]["identity"]
    source, target = ("target", "main") if plan["schema"] == 2 else ("main", "target")
    return {"from": identity[source]["uuid"], "to": identity[target]["uuid"], **item}


def compare_balances(plan, snapshot, completed):
    reserve_btc = number(plan["reserve_btc"])
    no_pending(snapshot)
    require(snapshot["identity"] == plan["snapshot"]["identity"], "포켓 연결이 계획과 다릅니다")
    expected = {role: totals(plan["snapshot"], role) for role in ("main", "target")}
    source, target = ("target", "main") if plan["schema"] == 2 else ("main", "target")
    for item in plan["items"]:
        if item["identifier"] in completed:
            code, quantity = item["currency"], number(item["amount"])
            expected[source][code] -= quantity
            expected[target][code] = expected[target].get(code, Decimal(0)) + quantity
    for role in ("main", "target"):
        actual = totals(snapshot, role)
        codes = set(actual) | set(expected[role])
        require(
            all(actual.get(c, Decimal(0)) == expected[role].get(c, Decimal(0)) for c in codes),
            "잔고가 계획 및 완료된 이전 내역과 다릅니다. 자동 보정하지 않습니다",
        )
    require(totals(snapshot, "main").get("BTC", Decimal(0)) >= reserve_btc, "메인 BTC 보존 확인 실패")


class Journal:
    """Append and fsync before POST. A missing receipt never permits a second POST."""

    def __init__(self, path, plan):
        self.path, self.plan = path, plan

    def __enter__(self):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.stream = os.fdopen(fd, "r+")
        try:
            info = os.fstat(fd)
            require(info.st_uid == os.getuid() and info.st_mode & 0o077 == 0, "실행 기록 파일 권한 오류")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            rows = [json.loads(line) for line in self.stream]
            if not rows:
                self.append({"plan_sha256": digest(self.plan)})
            else:
                require(rows[0] == {"plan_sha256": digest(self.plan)}, "실행 기록과 계획이 다릅니다")
            self.attempted = {row["attempt"] for row in rows[1:]}
            require(
                self.attempted <= {i["identifier"] for i in self.plan["items"]}, "실행 기록 식별자 불일치"
            )
            return self
        except BaseException:
            self.stream.close()
            raise

    def append(self, row):
        self.stream.seek(0, os.SEEK_END)
        self.stream.write(json.dumps(row) + "\n")
        self.stream.flush()
        os.fsync(self.stream.fileno())
        sync_directory(self.path)

    def attempt(self, identifier):
        require(identifier not in self.attempted, "이미 전송 시도한 이전 요청입니다")
        self.append({"attempt": identifier})
        self.attempted.add(identifier)

    def __exit__(self, *_):
        self.stream.close()


async def completed_transfers(client, plan, journal=None):
    completed = set()
    for item in plan["items"]:
        receipt = await client.receipt(plan, item)
        if receipt:
            require(
                receipt["state"] == "done",
                f"{item['currency']} 이전 {receipt['state']}: 완료 여부를 먼저 확인하세요",
            )
            completed.add(item["identifier"])
        elif journal and item["identifier"] in journal.attempted:
            raise MigrationError(
                "전송 시도 기록은 있지만 이전 내역이 없습니다. 재전송하지 말고 업비트에서 확인하세요"
            )
    return completed


async def migrate(client, plan, journal, check_ledger):
    validate_plan(plan)
    require(
        datetime.now(UTC) - datetime.fromisoformat(plan["created_at"]) < timedelta(hours=24),
        "계획이 24시간 지났습니다. 새 전송을 중단합니다",
    )
    completed = await completed_transfers(client, plan, journal)
    await check_ledger()
    compare_balances(plan, await client.snapshot(), completed)
    for item in plan["items"]:
        if item["identifier"] in completed:
            continue
        await check_ledger()
        compare_balances(plan, await client.snapshot(), completed)
        await check_ledger()
        journal.attempt(item["identifier"])
        # The only external mutation in this module. Never replayed after an attempt.
        await client.request("admin", TRANSFER, body=transfer_body(plan, item))
        receipt = await client.receipt(plan, item)
        require(
            receipt and receipt["state"] == "done", "이전 접수 후 완료 미확인: 같은 계획으로 다시 확인하세요"
        )
        completed.add(item["identifier"])
        compare_balances(plan, await client.snapshot(), completed)
    return completed


@contextlib.asynccontextmanager
async def ledger_guard(args):
    values = env_values(Path(args.database_env_file).expanduser())
    require(values.get("COTRADER_DATABASE_URL"), "DB 파일에 COTRADER_DATABASE_URL이 없습니다")
    url = make_url(values["COTRADER_DATABASE_URL"])
    require(
        url.drivername == "mysql+asyncmy" and url.database == args.expected_database,
        "의도한 Cotrader MySQL DB인지 확인하세요",
    )
    if args.db_host:
        require(
            args.db_host in {"127.0.0.1", "localhost", "::1"}, "DB 주소 변경은 로컬 포트 전달에만 허용합니다"
        )
        url = url.set(host=args.db_host, port=args.db_port)
    settings = Settings(runtime_role="research", database_url=url.render_as_string(hide_password=False))
    db, sessions = database(settings)
    try:
        async with SingleWriter(db, "cotrader:engine") as lock:
            row = (await lock.connection.execute(text("SELECT DATABASE(), @@server_uuid"))).one()
            await lock.connection.commit()
            require(row[0] == args.expected_database, "실제 DB 이름 불일치")
            db_identity = {"name": row[0], "server_uuid": row[1]}
            yield lock, sessions, db_identity
    finally:
        await db.dispose()


def engine_env(values):
    content = []
    for suffix in ("ACCESS_KEY", "SECRET_KEY"):
        value = values[PREFIXES["target"] + suffix]
        require(re.fullmatch(r"[A-Za-z0-9_-]+", value), "키 파일 출력에 지원하지 않는 문자가 있습니다")
        content.append(f"UPBIT_OPEN_API_{suffix}={value}\n")
    return "".join(content)


async def run(args):
    values = env_values(Path(args.key_file).expanduser())
    client = PocketClient(values, execute=args.action == "apply")
    try:
        if args.action == "inspect":
            print(json.dumps(await client.snapshot(), ensure_ascii=False, indent=2))
            return
        async with ledger_guard(args) as (lock, sessions, db_identity):
            if args.action == "plan":
                ledger = await ledger_evidence(sessions)
                snapshot = await client.snapshot()
                await lock.verify()
                require(ledger == await ledger_evidence(sessions), "조회 도중 장부가 바뀌었습니다")
                if args.direction == "to-main":
                    require(args.reserve_btc is None, "반환에는 --reserve-btc를 지정하지 않습니다")
                    plan = make_return_plan(snapshot, ledger, db_identity, args.exclude_currency)
                else:
                    require(not args.exclude_currency, "제외 자산은 반환 계획에서만 지정합니다")
                    plan = make_plan(snapshot, ledger, db_identity, args.reserve_btc)
                private_write(args.output, serialize(plan))
                print(
                    json.dumps(
                        {"plan": str(args.output), "sha256": digest(plan), "items": plan["items"]},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return
            plan = json.loads(private_read(args.plan))
            validate_plan(plan)
            require(db_identity == plan["database"], "계획과 실제 DB가 다릅니다")

            async def check_ledger():
                await lock.verify()
                require(
                    plan["ledger"] == await ledger_evidence(sessions), "전략 설정·보유 장부가 계획과 다릅니다"
                )

            await check_ledger()
            if args.action == "retire":
                require(plan["schema"] == 2, "메인포켓 반환 후에만 운용 종료할 수 있습니다")
                require(args.confirm == digest(plan), "검토한 반환 계획의 SHA256이 필요합니다")
            if args.action == "apply":
                require(args.confirm == digest(plan), "검토한 계획의 SHA256을 --confirm에 입력하세요")
                with Journal(str(args.plan) + ".journal", plan) as journal:
                    await migrate(client, plan, journal, check_ledger)
            completed = await completed_transfers(client, plan)
            require(len(completed) == len(plan["items"]), "모든 자산의 이전 완료가 확인되지 않았습니다")
            snapshot = await client.snapshot()
            await check_ledger()
            compare_balances(plan, snapshot, completed)
            if plan["schema"] == 1:
                require(
                    totals(snapshot, "main").get("BTC", Decimal(0)) == number(plan["reserve_btc"]),
                    "메인의 BTC가 계획한 보존 수량과 다릅니다",
                )
            if args.action == "verify" and args.engine_env:
                require(plan["schema"] == 1, "반환 후에는 실거래 엔진 키 파일을 생성하지 않습니다")
                private_write(args.engine_env, engine_env(values))
            if args.action == "retire":
                from cotrader.retirement import retire_returned
                from cotrader.upbit import UpbitBroker

                market = UpbitBroker(Settings(runtime_role="research", upbit_enabled=True))
                try:
                    symbols = sorted({r["symbol"] for r in plan["ledger"] if number(r["state"]["quantity"])})
                    quotes = {q.symbol: q for q in await market.orderbooks(symbols)} if symbols else {}
                    await lock.verify()
                    async with sessions.begin() as session:
                        retired = await retire_returned(session, plan, quotes, datetime.now(UTC))
                    print(json.dumps({"retired": retired, "actual_order_created": False}, ensure_ascii=False))
                finally:
                    await market.close()
            print(
                json.dumps(
                    {
                        "verified": True,
                        "main_btc": decimal_text(totals(snapshot, "main").get("BTC", Decimal(0))),
                        "direction": "to-main" if plan["schema"] == 2 else "to-sub",
                        "completed_assets": len(completed),
                        "orders_created": False,
                        "engine_resumed": False,
                    },
                    ensure_ascii=False,
                )
            )
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(description="사용자 실행용 업비트 포켓 이전 — 메인 BTC 보존 수량 지정")
    parser.add_argument("--key-file", default="~/.config/upbit/upbit-api-key.env")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("inspect", help="포켓·잔고·주문 조회만 수행")
    for action in ("plan", "apply", "verify", "retire"):
        sub = actions.add_parser(action)
        sub.add_argument("--database-env-file", required=True)
        sub.add_argument("--expected-database", required=True)
        sub.add_argument("--db-host")
        sub.add_argument("--db-port", type=int, default=3306)
        if action == "plan":
            sub.add_argument("--direction", choices=["to-sub", "to-main"], default="to-sub")
            sub.add_argument("--reserve-btc")
            sub.add_argument("--exclude-currency", action="append", default=[])
            sub.add_argument("--output", type=Path, required=True)
        else:
            sub.add_argument("--plan", type=Path, required=True)
            if action in {"apply", "retire"}:
                sub.add_argument("--confirm", required=True, help="계획 생성 시 출력된 SHA256")
        if action == "verify":
            sub.add_argument("--engine-env", type=Path, help="검증 후 코트레이더 키를 새 600 파일로 저장")
    args = parser.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        asyncio.run(run(args))
    except MigrationError as exc:
        parser.exit(1, f"중단: {exc}\n")
    except (OSError, ValueError, ArithmeticError, KeyError, TypeError, RuntimeError, SQLAlchemyError):
        parser.exit(
            1, "중단: 파일·응답·DB 또는 실행 잠금을 확인하세요. 민감한 예외 내용은 출력하지 않습니다.\n"
        )


if __name__ == "__main__":
    main()
