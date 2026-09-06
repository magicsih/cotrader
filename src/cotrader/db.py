from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cotrader.config import Settings


def database(settings: Settings):
    engine = create_async_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


class SingleWriter:
    """Dedicated MySQL connection; a lost lock stops this process permanently."""

    def __init__(self, engine, name: str):
        self.engine, self.name, self.connection = engine, name, None

    async def __aenter__(self):
        self.connection = await self.engine.connect()
        acquired = await self.connection.scalar(text("SELECT GET_LOCK(:name, 0)"), {"name": self.name})
        if acquired != 1:
            await self.connection.close()
            raise RuntimeError(f"다른 프로세스가 {self.name} 실행권을 보유하고 있습니다")
        await self.connection.commit()
        return self

    async def verify(self):
        if self.connection.invalidated:
            raise RuntimeError("DB 실행권 연결이 끊겼습니다")
        owned = await self.connection.scalar(
            text("SELECT IS_USED_LOCK(:name) = CONNECTION_ID()"), {"name": self.name}
        )
        await self.connection.commit()
        if owned != 1:
            raise RuntimeError("DB 실행권을 잃었습니다")

    async def __aexit__(self, *_):
        if self.connection:
            try:
                if not self.connection.invalidated:
                    await self.connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": self.name})
                    await self.connection.commit()
            except SQLAlchemyError:
                await self.connection.invalidate()
            finally:
                await self.connection.close()
