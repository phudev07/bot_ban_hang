import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import release_session_connection


def test_release_session_connection_prevents_nested_pool_deadlock(tmp_path) -> None:
    async def scenario() -> None:
        database_path = (tmp_path / "pool-release.db").as_posix()
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{database_path}",
            pool_size=1,
            max_overflow=0,
            pool_timeout=0.05,
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as outer_session:
            await outer_session.execute(text("SELECT 1"))

            with pytest.raises(SqlAlchemyTimeoutError):
                async with sessions() as blocked_session:
                    await blocked_session.execute(text("SELECT 1"))

            await release_session_connection(outer_session)
            async with sessions() as nested_session:
                assert int(await nested_session.scalar(text("SELECT 1")) or 0) == 1
        await engine.dispose()

    asyncio.run(scenario())
