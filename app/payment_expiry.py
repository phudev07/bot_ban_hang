import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Deposit


logger = logging.getLogger(__name__)


def _message_ids(value: str) -> tuple[int, ...]:
    result: list[int] = []
    for item in value.split(","):
        try:
            message_id = int(item.strip())
        except ValueError:
            continue
        if message_id > 0 and message_id not in result:
            result.append(message_id)
    return tuple(result)


async def register_deposit_message(
    session: AsyncSession,
    deposit_id: int,
    chat_id: int,
    message_id: int,
) -> None:
    deposit = await session.scalar(
        select(Deposit).where(Deposit.id == deposit_id).with_for_update()
    )
    if deposit is None:
        return
    ids = list(_message_ids(deposit.telegram_message_ids))
    if message_id not in ids:
        ids.append(message_id)
    deposit.telegram_chat_id = chat_id
    deposit.telegram_message_ids = ",".join(str(item) for item in ids)
    deposit.messages_deleted_at = None
    await session.commit()


async def expire_pending_deposits(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    batch_size: int = 100,
) -> int:
    current_time = now or datetime.now(UTC)
    async with session_factory() as session:
        async with session.begin():
            deposits = list(
                await session.scalars(
                    select(Deposit)
                    .where(
                        Deposit.status == "pending",
                        Deposit.expires_at.is_not(None),
                        Deposit.expires_at <= current_time,
                    )
                    .order_by(Deposit.expires_at, Deposit.id)
                    .with_for_update(skip_locked=True)
                    .limit(max(1, batch_size))
                )
            )
            for deposit in deposits:
                deposit.status = "failed"
                deposit.failure_reason = "expired"
                deposit.failed_at = current_time
    return len(deposits)


@dataclass(frozen=True)
class DepositMessageCleanup:
    deposit_id: int
    chat_id: int
    message_ids: tuple[int, ...]


async def pending_deposit_message_cleanups(
    session: AsyncSession,
    *,
    batch_size: int = 100,
) -> list[DepositMessageCleanup]:
    deposits = list(
        await session.scalars(
            select(Deposit)
            .where(
                Deposit.status.in_(("paid", "failed")),
                Deposit.telegram_chat_id.is_not(None),
                Deposit.telegram_message_ids != "",
                Deposit.messages_deleted_at.is_(None),
            )
            .order_by(Deposit.id)
            .with_for_update(skip_locked=True)
            .limit(max(1, batch_size))
        )
    )
    return [
        DepositMessageCleanup(
            deposit_id=deposit.id,
            chat_id=int(deposit.telegram_chat_id),
            message_ids=_message_ids(deposit.telegram_message_ids),
        )
        for deposit in deposits
        if deposit.telegram_chat_id is not None and _message_ids(deposit.telegram_message_ids)
    ]


async def cleanup_deposit_messages(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    *,
    now: datetime | None = None,
    batch_size: int = 100,
) -> int:
    current_time = now or datetime.now(UTC)
    cleaned = 0
    async with session_factory() as session:
        async with session.begin():
            cleanups = await pending_deposit_message_cleanups(
                session,
                batch_size=batch_size,
            )
            for cleanup in cleanups:
                all_deleted = True
                for message_id in cleanup.message_ids:
                    try:
                        await bot.delete_message(cleanup.chat_id, message_id)
                    except (TelegramBadRequest, TelegramForbiddenError):
                        # Missing/inaccessible messages are already gone from the user's view.
                        continue
                    except Exception:
                        all_deleted = False
                        logger.exception(
                            "Could not delete payment message deposit=%s chat=%s message=%s",
                            cleanup.deposit_id,
                            cleanup.chat_id,
                            message_id,
                        )
                if not all_deleted:
                    continue
                deposit = await session.get(Deposit, cleanup.deposit_id)
                if deposit is not None:
                    deposit.messages_deleted_at = current_time
                    cleaned += 1
    return cleaned


async def payment_expiry_worker(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    interval_seconds: int,
) -> None:
    while True:
        try:
            await expire_pending_deposits(session_factory)
            await cleanup_deposit_messages(session_factory, bot)
        except Exception:
            logger.exception("Could not expire payment requests or clean up QR messages")
        await asyncio.sleep(max(1, interval_seconds))
