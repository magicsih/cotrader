import copy
import hashlib
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from cotrader.domain import ACTIVE_ORDERS
from cotrader.models import Command, Intent, Strategy
from cotrader.pockets import (
    PREFIXES,
    TRANSFER,
    Journal,
    MigrationError,
    PocketClient,
    digest,
    engine_env,
    env_values,
    ledger_evidence,
    make_plan,
    migrate,
    number,
    private_read,
    private_write,
    serialize,
    transfer_body,
    validate_plan,
)

# Independent synthetic balances and identities, never copied from account records.
MAIN = "00000000-0000-4000-8000-000000000001"
TARGET = "00000000-0000-4000-8000-000000000002"
VALUES = {
    prefix + suffix: f"fixture-{role}-{suffix}"
    for role, prefix in PREFIXES.items()
    for suffix in ("ACCESS_KEY", "SECRET_KEY")
}
LEDGER = [
    {
        "id": "strategy-fixture",
        "symbol": "USDT-ETH",
        "status": "PAUSED",
        "version": 1,
        "config": {"execution_policy": "maker_only"},
        "state": {"funded": True, "quantity": "3"},
    }
]


def snapshot():
    return {
        "identity": {
            "main": {"uuid": MAIN, "name": "Main"},
            "admin": {"uuid": MAIN, "name": "Main"},
            "target": {"uuid": TARGET, "name": "Cotrader"},
        },
        "balances": {
            "main": {
                "BTC": {"balance": "2.25", "locked": "0"},
                "ETH": {"balance": "3", "locked": "0"},
                "KRW": {"balance": "0.123456789", "locked": "0"},
            },
            "target": {},
        },
        "orders": {"main": [], "target": []},
    }


def plan():
    return make_plan(
        snapshot(), copy.deepcopy(LEDGER), {"name": "cotrader_test", "server_uuid": "fixture"}, "1"
    )


class Exchange:
    def __init__(self):
        self.data, self.rows, self.posts = snapshot(), {}, []
        self.timeout, self.processing, self.apply_balances = False, False, True

    async def snapshot(self):
        return copy.deepcopy(self.data)

    async def receipt(self, plan, item):
        return self.rows.get(item["identifier"])

    async def request(self, role, path, *, body):
        assert role == "admin" and path == TRANSFER
        self.posts.append(body)
        if self.apply_balances:
            code = body["currency"]
            source = self.data["balances"]["main"][code]
            source["balance"] = str(number(source["balance"]) - number(body["amount"]))
            self.data["balances"]["target"][code] = {"balance": body["amount"], "locked": "0"}
            self.rows[body["identifier"]] = {
                "uuid": "transfer-fixture",
                "state": "processing" if self.processing else "done",
            }
        if self.timeout:
            self.timeout = False
            raise MigrationError("timeout fixture")
        return self.rows.get(body["identifier"])


def test_plan_preserves_reserve_without_rounding_dust():
    p = plan()
    assert {i["currency"]: i["amount"] for i in p["items"]} == {
        "BTC": "1.25",
        "ETH": "3",
        "KRW": "0.123456789",
    }
    validate_plan(p)
    p["items"][0]["amount"] = "2.25"
    with pytest.raises(MigrationError, match="원본 잔고"):
        validate_plan(p)


@pytest.mark.parametrize("change", ["under_reserve", "locked", "orders", "nonempty", "funded_btc"])
def test_unsafe_plan_is_rejected(change):
    s, ledger = snapshot(), copy.deepcopy(LEDGER)
    if change == "under_reserve":
        s["balances"]["main"]["BTC"]["balance"] = "0.9"
    if change == "locked":
        s["balances"]["main"]["ETH"]["locked"] = "1"
    if change == "orders":
        s["orders"]["target"] = [{"uuid": "foreign"}]
    if change == "nonempty":
        s["balances"]["target"] = {"BTC": {"balance": "1", "locked": "0"}}
    if change == "funded_btc":
        ledger[0]["symbol"] = "USDT-BTC"
    with pytest.raises(MigrationError):
        make_plan(s, ledger, {}, "1")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", 0.25])
def test_invalid_amounts_are_rejected(value):
    with pytest.raises(MigrationError):
        number(value)


def test_private_files_do_not_overwrite_follow_symlinks_or_accept_loose_permissions(tmp_path):
    file = tmp_path / "key.env"
    private_write(file, "TOKEN=fixture\n")
    assert file.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        private_write(file, "replacement")
    link = tmp_path / "link"
    link.symlink_to(file)
    with pytest.raises(OSError):
        private_read(link)
    file.chmod(0o644)
    with pytest.raises(MigrationError):
        private_read(file)


def test_env_parser_never_expands_shell_commands_and_rejects_duplicates(tmp_path):
    file = tmp_path / "key.env"
    private_write(file, "export TOKEN='$(exit 1)'\n")
    assert env_values(file) == {"TOKEN": "$(exit 1)"}
    file.write_text("TOKEN=a\nTOKEN=b\n")
    with pytest.raises(MigrationError, match="중복"):
        env_values(file)
    mapped = engine_env(VALUES)
    assert mapped.count("\n") == 2
    assert VALUES["UPBIT_OPEN_API_COTRADER_ACCESS_KEY"] in mapped
    assert VALUES["UPBIT_OPEN_API_ACCESS_KEY"] not in mapped
    assert VALUES["UPBIT_OPEN_API_POCKET_ADMIN_ACCESS_KEY"] not in mapped


async def test_migration_journals_before_post_and_verifies_each_asset(tmp_path):
    p, exchange, check = plan(), Exchange(), AsyncMock()
    with Journal(tmp_path / "journal", p) as journal:
        original = exchange.request

        async def recorded(role, path, *, body):
            assert body["identifier"] in (tmp_path / "journal").read_text()
            return await original(role, path, body=body)

        exchange.request = recorded
        done = await migrate(exchange, p, journal, check)
    assert len(done) == 3 and len(exchange.posts) == 3
    assert exchange.data["balances"]["main"]["BTC"]["balance"] == "1.00"
    with Journal(tmp_path / "journal", p) as journal:
        await migrate(exchange, p, journal, check)
    assert len(exchange.posts) == 3
    assert check.await_count >= 4


async def test_timeout_after_success_recovers_by_identifier_without_second_post(tmp_path):
    p, exchange = plan(), Exchange()
    exchange.timeout = True
    with Journal(tmp_path / "journal", p) as journal, pytest.raises(MigrationError, match="timeout"):
        await migrate(exchange, p, journal, AsyncMock())
    with Journal(tmp_path / "journal", p) as journal:
        await migrate(exchange, p, journal, AsyncMock())
    assert len(exchange.posts) == 3
    assert len({r["identifier"] for r in exchange.posts}) == 3


async def test_attempt_without_receipt_is_never_retransmitted(tmp_path):
    p, exchange = plan(), Exchange()
    exchange.timeout, exchange.apply_balances = True, False
    with Journal(tmp_path / "journal", p) as journal, pytest.raises(MigrationError):
        await migrate(exchange, p, journal, AsyncMock())
    with Journal(tmp_path / "journal", p) as journal, pytest.raises(MigrationError, match="재전송하지"):
        await migrate(exchange, p, journal, AsyncMock())
    assert len(exchange.posts) == 1


async def test_processing_receipt_blocks_remaining_assets_until_done(tmp_path):
    p, exchange = plan(), Exchange()
    exchange.processing = True
    with Journal(tmp_path / "journal", p) as journal, pytest.raises(MigrationError, match="완료 미확인"):
        await migrate(exchange, p, journal, AsyncMock())
    with Journal(tmp_path / "journal", p) as journal, pytest.raises(MigrationError, match="processing"):
        await migrate(exchange, p, journal, AsyncMock())
    assert len(exchange.posts) == 1
    for row in exchange.rows.values():
        row["state"] = "done"
    exchange.processing = False
    with Journal(tmp_path / "journal", p) as journal:
        await migrate(exchange, p, journal, AsyncMock())
    assert len(exchange.posts) == 3


async def test_balance_drift_or_lost_lock_prevents_transfer(tmp_path):
    p, exchange = plan(), Exchange()
    exchange.data["balances"]["main"]["ETH"]["balance"] = "2.9"
    with Journal(tmp_path / "journal", p) as journal, pytest.raises(MigrationError, match="잔고"):
        await migrate(exchange, p, journal, AsyncMock())
    exchange.data = snapshot()
    with Journal(tmp_path / "journal", p) as journal, pytest.raises(MigrationError, match="lock lost"):
        await migrate(exchange, p, journal, AsyncMock(side_effect=MigrationError("lock lost")))
    assert not exchange.posts


def test_journal_excludes_concurrent_process_and_other_plan(tmp_path):
    p, file = plan(), tmp_path / "journal"
    with Journal(file, p), pytest.raises(BlockingIOError), Journal(file, p):
        pass
    with pytest.raises(MigrationError, match="기록과 계획"), Journal(file, plan()):
        pass
    file.write_text(file.read_text() + '{"attempt":')
    with pytest.raises(json.JSONDecodeError), Journal(file, p):
        pass


async def test_client_transport_allows_only_scoped_transfer_and_gets():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(
        base_url="https://api.upbit.com", transport=httpx.MockTransport(handler)
    ) as http:
        client = PocketClient(VALUES, client=http)
        with pytest.raises(MigrationError, match="조회 전용"):
            await client.request("admin", TRANSFER, body={})
        for role, endpoint in [
            ("main", TRANSFER),
            ("target", TRANSFER),
            ("admin", "/v1/orders"),
            ("admin", "/v1/withdraws"),
        ]:
            with pytest.raises(MigrationError):
                await client.request(role, endpoint, body={})
        assert calls == []
        await client.request("main", "/v1/accounts")
        client.execute = True
        await client.request("admin", TRANSFER, body=transfer_body(plan(), plan()["items"][0]))
        assert [c.method for c in calls] == ["GET", "POST"]


@pytest.mark.parametrize("mismatch", ["amount", "from", "to", "identifier", "currency", "multiple"])
async def test_receipt_identity_and_amount_are_checked(mismatch):
    p = plan()
    body = transfer_body(p, p["items"][0])
    row = {**body, "uuid": "receipt-fixture", "state": "done"}
    if mismatch != "multiple":
        row[mismatch] = "999" if mismatch == "amount" else "different"
    async with httpx.AsyncClient(
        base_url="https://api.upbit.com",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=[row, row] if mismatch == "multiple" else [row])
        ),
    ) as http:
        with pytest.raises(MigrationError):
            await PocketClient(VALUES, client=http).receipt(p, p["items"][0])


async def test_ledger_requires_paused_strategies_terminal_orders_and_no_queued_commands(db):
    _, sessions = db
    async with sessions.begin() as session:
        row = Strategy(
            id="s",
            name="synthetic",
            venue="upbit_usdt",
            symbol="USDT-ETH",
            mode="live",
            status="RUNNING",
            config={},
            state={},
        )
        session.add(row)
    with pytest.raises(MigrationError, match="전략"):
        await ledger_evidence(sessions)
    async with sessions.begin() as session:
        row = await session.get(Strategy, "s")
        row.status = "PAUSED"
        session.add(
            Intent(
                id="i",
                strategy_id="s",
                mode="live",
                venue="upbit_usdt",
                symbol="USDT-ETH",
                side="SELL",
                quantity=1,
                price=2000,
            )
        )
    for status in ACTIVE_ORDERS:
        async with sessions.begin() as session:
            (await session.get(Intent, "i")).status = status
        with pytest.raises(MigrationError, match="장부"):
            await ledger_evidence(sessions)
    async with sessions.begin() as session:
        (await session.get(Intent, "i")).status = "CANCELED"
        session.add(Command(id="c", action="start", payload={}, actor="fixture"))
    with pytest.raises(MigrationError, match="명령"):
        await ledger_evidence(sessions)
    async with sessions.begin() as session:
        (await session.get(Command, "c")).status = "REJECTED"
    before = await ledger_evidence(sessions)
    assert before[0]["status"] == "PAUSED"
    assert before == await ledger_evidence(sessions)
    async with sessions() as session:
        assert (await session.scalar(select(Intent))).status == "CANCELED"


@pytest.mark.parametrize(
    "bad_role", [None, "target_type", "admin_permission", "target_permission", "source_identity"]
)
async def test_identity_resolves_roles_without_returning_keys(bad_role):
    pockets = [
        {"uuid": MAIN, "name": "Main", "type": "main"},
        {"uuid": TARGET, "name": "Cotrader", "type": "user_spot_trading"},
    ]
    keys = [
        {
            "uuid": MAIN,
            "keys": [
                {
                    "access_key": VALUES[PREFIXES["main"] + "ACCESS_KEY"],
                    "permissions": ["view_account", "view_orders"],
                },
                {"access_key": VALUES[PREFIXES["admin"] + "ACCESS_KEY"], "permissions": ["manage_pockets"]},
            ],
        },
        {
            "uuid": TARGET,
            "keys": [
                {
                    "access_key": VALUES[PREFIXES["target"] + "ACCESS_KEY"],
                    "permissions": ["view_account", "view_orders", "make_orders"],
                }
            ],
        },
    ]
    if bad_role == "target_type":
        pockets[1]["type"] = "main"
    if bad_role == "admin_permission":
        keys[0]["keys"][1]["permissions"] = []
    if bad_role == "target_permission":
        keys[1]["keys"][0]["permissions"] = ["view_account"]
    if bad_role == "source_identity":
        keys[1]["keys"].append(keys[0]["keys"].pop(0))

    def handler(request):
        return httpx.Response(200, json=keys if request.url.path.endswith("api_keys") else pockets)

    async with httpx.AsyncClient(
        base_url="https://api.upbit.com", transport=httpx.MockTransport(handler)
    ) as http:
        client = PocketClient(VALUES, client=http)
        if bad_role:
            with pytest.raises(MigrationError):
                await client.identity()
        else:
            result = await client.identity()
            assert result == snapshot()["identity"]
            assert not any(v in json.dumps(result) for v in VALUES.values())


@pytest.mark.parametrize("status", [429, 418, 400, 500])
async def test_http_failures_do_not_retry_or_expose_response_body(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            json={
                "error": {"name": "currency_not_found", "message": VALUES[PREFIXES["admin"] + "SECRET_KEY"]}
            },
        )

    async with httpx.AsyncClient(
        base_url="https://api.upbit.com", transport=httpx.MockTransport(handler)
    ) as http:
        with pytest.raises(MigrationError) as error:
            await PocketClient(VALUES, client=http, execute=True).request("admin", TRANSFER, body={})
    assert len(calls) == 1
    assert not any(v in str(error.value) for v in VALUES.values())


async def test_lock_lost_during_final_snapshot_stops_before_post(tmp_path):
    p, exchange = plan(), Exchange()
    check = AsyncMock(side_effect=[None, None, MigrationError("lock lost")])
    with Journal(tmp_path / "journal", p) as journal, pytest.raises(MigrationError, match="lock lost"):
        await migrate(exchange, p, journal, check)
    assert not exchange.posts
    assert '"attempt"' not in (tmp_path / "journal").read_text()


async def test_real_mysql_lock_excludes_running_engine(db):
    from cotrader.db import SingleWriter

    engine, _ = db
    if engine.dialect.name != "mysql":
        pytest.skip("격리 MySQL 실행에서만 실제 실행 잠금 검증")
    async with SingleWriter(engine, "cotrader:engine") as owner:
        await owner.verify()
        with pytest.raises(RuntimeError, match="실행권"):
            async with SingleWriter(engine, "cotrader:engine"):
                pytest.fail("동일 엔진 잠금을 두 번 획득함")
    async with SingleWriter(engine, "cotrader:engine") as operator:
        await operator.verify()


def test_confirmation_digest_matches_saved_file(tmp_path):
    p = plan()
    file = tmp_path / "plan.json"
    private_write(file, serialize(p))
    assert hashlib.sha256(file.read_bytes()).hexdigest() == digest(p)
