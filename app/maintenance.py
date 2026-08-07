import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator
from weakref import WeakKeyDictionary

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import FeatureFlag


SMS_RENTAL_MAINTENANCE = "sms_rental_maintenance"
_sms_maintenance_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    WeakKeyDictionary()
)


def _sms_maintenance_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _sms_maintenance_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _sms_maintenance_locks[loop] = lock
    return lock


async def feature_enabled(session: AsyncSession, key: str) -> bool:
    value = await session.scalar(select(FeatureFlag.enabled).where(FeatureFlag.key == key))
    return bool(value)


async def sms_rental_maintenance_enabled(session: AsyncSession) -> bool:
    return await feature_enabled(session, SMS_RENTAL_MAINTENANCE)


async def ensure_sms_rental_maintenance(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    enabled_by_default: bool,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            flag = await session.get(FeatureFlag, SMS_RENTAL_MAINTENANCE)
            if flag is None:
                session.add(
                    FeatureFlag(
                        key=SMS_RENTAL_MAINTENANCE,
                        enabled=enabled_by_default,
                    )
                )


async def set_sms_rental_maintenance(
    session: AsyncSession,
    enabled: bool,
) -> None:
    flag = await session.scalar(
        select(FeatureFlag)
        .where(FeatureFlag.key == SMS_RENTAL_MAINTENANCE)
        .with_for_update()
    )
    if flag is None:
        session.add(FeatureFlag(key=SMS_RENTAL_MAINTENANCE, enabled=enabled))
    else:
        flag.enabled = enabled


@asynccontextmanager
async def sms_maintenance_operation() -> AsyncIterator[None]:
    async with _sms_maintenance_lock():
        yield
