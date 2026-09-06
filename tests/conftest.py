import os

import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cotrader.models import Base


@pytest_asyncio.fixture
async def db():
    url = os.environ.get("COTRADER_TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" and (
        parsed.host != "127.0.0.1" or parsed.database != "cotrader_test"
    ):
        pytest.fail("통합 테스트는 127.0.0.1의 cotrader_test DB에서만 실행할 수 있습니다")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield engine, async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()
