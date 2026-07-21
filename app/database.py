from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def create_database(
    database_url: str,
    *,
    pool_size: int = 10,
    max_overflow: int = 10,
    pool_timeout: float = 8.0,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=max(1, int(pool_size)),
        max_overflow=max(0, int(max_overflow)),
        pool_timeout=max(1.0, float(pool_timeout)),
        pool_use_lifo=True,
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def release_session_connection(session: AsyncSession) -> None:
    """Release a read transaction before a handler starts long-running external work."""
    if session.in_transaction():
        if session.new or session.dirty or session.deleted:
            raise RuntimeError("Cannot release a session that has pending database changes")
        await session.commit()


class DatabaseSessionMiddleware(BaseMiddleware):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self.session_factory() as session:
            data["session"] = session
            data["session_factory"] = self.session_factory
            return await handler(event, data)
