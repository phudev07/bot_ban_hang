import asyncio
import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BroadcastLog, User


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BroadcastResult:
    total: int
    delivered: int
    failed: int


def broadcast_purchase_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 Mua ngay",
                    callback_data="menu:products",
                )
            ]
        ]
    )


def is_unreachable_error(exc: Exception) -> bool:
    if isinstance(exc, TelegramForbiddenError):
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in ("bot was blocked", "chat not found", "user is deactivated")
    )


async def deliver_broadcast(
    session: AsyncSession,
    bot: Bot,
    *,
    admin_id: int,
    source_chat_id: int,
    source_message_id: int,
    throttle_seconds: float = 0.05,
) -> BroadcastResult:
    recipient_ids = list(
        await session.scalars(
            select(User.telegram_id)
            .where(User.has_started.is_(True))
            .order_by(User.telegram_id)
        )
    )
    delivered = 0
    failed = 0
    inactive_ids: list[int] = []
    purchase_keyboard = broadcast_purchase_keyboard()

    for user_id in recipient_ids:
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id,
                reply_markup=purchase_keyboard,
            )
        except TelegramRetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.2)
            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=source_chat_id,
                    message_id=source_message_id,
                    reply_markup=purchase_keyboard,
                )
            except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter) as retry_exc:
                failed += 1
                if is_unreachable_error(retry_exc):
                    inactive_ids.append(user_id)
            except Exception:
                failed += 1
                logger.exception("Unexpected broadcast retry failure for user %s", user_id)
            else:
                delivered += 1
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            failed += 1
            if is_unreachable_error(exc):
                inactive_ids.append(user_id)
        except Exception:
            failed += 1
            logger.exception("Unexpected broadcast failure for user %s", user_id)
        else:
            delivered += 1

        if throttle_seconds > 0:
            await asyncio.sleep(throttle_seconds)

    if inactive_ids:
        await session.execute(
            update(User)
            .where(User.telegram_id.in_(inactive_ids))
            .values(has_started=False)
        )
    session.add(
        BroadcastLog(
            admin_id=admin_id,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            total_recipients=len(recipient_ids),
            delivered_count=delivered,
            failed_count=failed,
        )
    )
    await session.commit()
    return BroadcastResult(
        total=len(recipient_ids),
        delivered=delivered,
        failed=failed,
    )
