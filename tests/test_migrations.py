import os
import subprocess
import sys

import pytest
from sqlalchemy import text

from cotrader.config import Settings
from cotrader.models import AccountState, Base, CandleRow, RuntimeState, Strategy
from cotrader.services import bootstrap


@pytest.mark.skipif(not os.environ.get("COTRADER_TEST_DATABASE_URL"), reason="isolated MySQL required")
async def test_venue_upgrade_preserves_existing_usd_records(db):
    engine, sessions = db  # Fixture rejects every non-local or non-test database.
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    env = {**os.environ, "COTRADER_DATABASE_URL": engine.url.render_as_string(hide_password=False)}

    def migrate(revision):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", revision], env=env, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    migrate("f64ab092713e")
    async with engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO account_states VALUES ('paper',5000,5123,5100,'2026-09-01',1,'loss')")
        )
        await connection.execute(
            text("INSERT INTO runtime_state VALUES ('portfolio:paper',:data,'2026-09-01')"),
            {"data": '{"equity":"4987"}'},
        )
        await connection.execute(
            text("INSERT INTO candles VALUES ('old-bar','AAPL','1m','2026-09-01',:data,'toss')"),
            {"data": '{"closePrice":"200"}'},
        )
        await connection.execute(
            text(
                "INSERT INTO strategies VALUES ('old-strategy','saved','AAPL','paper','PAUSED',3,:config,:state,'held','2026-09-01')"
            ),
            {"config": '{"budget":"1000"}', "state": '{"cash":"123","quantity":"4"}'},
        )
    migrate("head")
    async with sessions.begin() as session:
        await bootstrap(session, Settings())
        stock = await session.get(AccountState, {"venue": "toss", "mode": "paper"})
        coin = await session.get(AccountState, {"venue": "upbit", "mode": "paper"})
        assert (stock.capital, stock.high_water, stock.daily_anchor, stock.halted) == (5000, 5123, 5100, True)
        assert stock.reason == "loss" and coin.capital == 1000000 and not coin.halted
        assert await session.get(RuntimeState, "portfolio:paper") is None
        assert (await session.get(RuntimeState, "portfolio:toss:paper")).data == {"equity": "4987"}
        bar = await session.get(CandleRow, "old-bar")
        assert bar.venue == "toss" and bar.data == {"closePrice": "200"}
        strategy = await session.get(Strategy, "old-strategy")
        assert strategy.venue == "toss" and strategy.version == 3 and strategy.status == "PAUSED"
        assert strategy.state == {"cash": "123", "quantity": "4"}
