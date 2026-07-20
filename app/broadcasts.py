import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    BroadcastDelivery,
    BroadcastLog,
    Product,
    ProductPriceAlert,
    ProductStockAlert,
    User,
)
from app.stock_alerts import stock_alert_enabled
from app.utils import format_vnd, safe_html


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BroadcastResult:
    total: int
    delivered: int
    failed: int


@dataclass(frozen=True)
class QueuedBroadcast:
    broadcast_id: int
    total: int


@dataclass(frozen=True)
class DeliveryResult:
    delivery_id: int
    user_id: int
    delivered: bool
    inactive: bool = False
    error: str | None = None


@dataclass(frozen=True)
class BroadcastPayload:
    broadcast_id: int
    source_chat_id: int
    source_message_id: int


class BroadcastRateLimiter:
    def __init__(self, rate_per_second: int) -> None:
        self.interval = 1 / max(1, rate_per_second)
        self._lock = asyncio.Lock()
        self._next_send_at = 0.0
        self._paused_until = 0.0

    async def wait(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            async with self._lock:
                now = loop.time()
                send_at = max(now, self._next_send_at, self._paused_until)
                self._next_send_at = send_at + self.interval
            delay = send_at - now
            if delay > 0:
                await asyncio.sleep(delay)
            async with self._lock:
                if self._paused_until <= send_at:
                    return

    async def pause(self, seconds: float) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            self._paused_until = max(
                self._paused_until,
                loop.time() + max(0.0, seconds),
            )
            self._next_send_at = max(self._next_send_at, self._paused_until)


@dataclass(frozen=True)
class SaleAlertPayload:
    alert_id: int
    product_id: int
    name_vi: str
    name_en: str
    old_price: int
    new_price: int
    stock: int
    recipients: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class StockAlertPayload:
    alert_id: int
    product_id: int
    name_vi: str
    name_en: str
    price: int
    stock: int
    recipients: tuple[tuple[int, str], ...]


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


def sale_purchase_keyboard(product_id: int, price: int, language: str) -> InlineKeyboardMarkup:
    label = (
        f"🛒 Mua ngay · {format_vnd(price)}"
        if language == "vi"
        else f"🛒 Buy now · {format_vnd(price)}"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"prod:{product_id}")]
        ]
    )


def sale_alert_text(payload: SaleAlertPayload, language: str) -> str:
    if language == "en":
        return (
            "🔥 <b>PRODUCT PRICE DROP</b>\n\n"
            f"• Product: <b>{safe_html(payload.name_en)}</b>\n"
            f"• Previous price: <s>{format_vnd(payload.old_price)}</s>\n"
            f"• Sale price: <b>{format_vnd(payload.new_price)}</b>\n"
            f"• Available now: <b>{payload.stock}</b>\n\n"
            "This is the shop's current selling price. Buy now before the price changes."
        )
    return (
        "🔥 <b>MẶT HÀNG VỪA GIẢM GIÁ</b>\n\n"
        f"• Sản phẩm: <b>{safe_html(payload.name_vi)}</b>\n"
        f"• Giá trước: <s>{format_vnd(payload.old_price)}</s>\n"
        f"• Giá sale còn: <b>{format_vnd(payload.new_price)}</b>\n"
        f"• Kho hiện có: <b>{payload.stock}</b>\n\n"
        "Đây là giá bán hiện tại của shop. Mua ngay trước khi giá thay đổi."
    )


def stock_alert_text(payload: StockAlertPayload, language: str) -> str:
    if language == "en":
        return (
            "📦 <b>PRODUCT BACK IN STOCK</b>\n\n"
            f"• Product: <b>{safe_html(payload.name_en)}</b>\n"
            f"• Current price: <b>{format_vnd(payload.price)}</b>\n"
            f"• Available now: <b>{payload.stock}</b>\n\n"
            "Stock can sell out quickly. Buy now while it is available."
        )
    return (
        "📦 <b>HÀNG MỚI VỀ</b>\n\n"
        f"• Sản phẩm: <b>{safe_html(payload.name_vi)}</b>\n"
        f"• Giá hiện tại: <b>{format_vnd(payload.price)}</b>\n"
        f"• Kho vừa có: <b>{payload.stock}</b>\n\n"
        "Số lượng có thể hết nhanh. Mua ngay khi hàng đang còn."
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
    started_at = datetime.now(UTC)
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
            status="completed",
            started_at=started_at,
            completed_at=datetime.now(UTC),
        )
    )
    await session.commit()
    return BroadcastResult(
        total=len(recipient_ids),
        delivered=delivered,
        failed=failed,
    )


async def queue_broadcast(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    admin_id: int,
    source_chat_id: int,
    source_message_id: int,
) -> QueuedBroadcast:
    async with session_factory() as session:
        async with session.begin():
            recipient_ids = list(
                await session.scalars(
                    select(User.telegram_id)
                    .where(User.has_started.is_(True))
                    .order_by(User.telegram_id)
                )
            )
            campaign = BroadcastLog(
                admin_id=admin_id,
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
                total_recipients=len(recipient_ids),
                status="queued",
            )
            session.add(campaign)
            await session.flush()
            session.add_all(
                BroadcastDelivery(
                    broadcast_id=campaign.id,
                    user_id=user_id,
                    status="pending",
                )
                for user_id in recipient_ids
            )
        return QueuedBroadcast(campaign.id, len(recipient_ids))


async def recover_interrupted_broadcasts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(BroadcastDelivery)
                .where(BroadcastDelivery.status == "sending")
                .values(status="pending")
            )
            await session.execute(
                update(BroadcastLog)
                .where(BroadcastLog.status == "sending")
                .values(status="queued")
            )


async def _claim_broadcast(
    session_factory: async_sessionmaker[AsyncSession],
) -> BroadcastPayload | None:
    async with session_factory() as session:
        async with session.begin():
            campaign = await session.scalar(
                select(BroadcastLog)
                .where(BroadcastLog.status.in_(("queued", "sending")))
                .order_by(BroadcastLog.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if campaign is None:
                return None
            campaign.status = "sending"
            campaign.started_at = campaign.started_at or datetime.now(UTC)
            campaign.last_error = None
            return BroadcastPayload(
                broadcast_id=campaign.id,
                source_chat_id=campaign.source_chat_id,
                source_message_id=campaign.source_message_id,
            )


async def _claim_delivery_batch(
    session_factory: async_sessionmaker[AsyncSession],
    broadcast_id: int,
    batch_size: int,
) -> list[BroadcastDelivery]:
    async with session_factory() as session:
        async with session.begin():
            deliveries = list(
                await session.scalars(
                    select(BroadcastDelivery)
                    .where(
                        BroadcastDelivery.broadcast_id == broadcast_id,
                        BroadcastDelivery.status == "pending",
                    )
                    .order_by(BroadcastDelivery.id)
                    .with_for_update(skip_locked=True)
                    .limit(batch_size)
                )
            )
            for delivery in deliveries:
                delivery.status = "sending"
                delivery.attempt_count += 1
                delivery.last_error = None
            await session.flush()
            return deliveries


async def _send_with_retry(
    operation: Callable[[], Awaitable[object]],
    limiter: BroadcastRateLimiter,
    semaphore: asyncio.Semaphore,
    *,
    max_attempts: int = 3,
) -> tuple[bool, bool, str | None]:
    async with semaphore:
        for attempt in range(max_attempts):
            await limiter.wait()
            try:
                await operation()
                return True, False, None
            except TelegramRetryAfter as exc:
                retry_after = float(exc.retry_after) + 0.25
                await limiter.pause(retry_after)
                if attempt + 1 >= max_attempts:
                    return False, False, f"telegram_retry_after:{exc.retry_after}"
            except (TelegramBadRequest, TelegramForbiddenError) as exc:
                return False, is_unreachable_error(exc), str(exc)[:500]
            except Exception as exc:
                if attempt + 1 >= max_attempts:
                    logger.exception("Unexpected broadcast delivery failure")
                    return False, False, str(exc)[:500]
                await asyncio.sleep(0.25 * (2**attempt))
    return False, False, "broadcast_delivery_failed"


async def _send_queued_delivery(
    bot: Bot,
    payload: BroadcastPayload,
    delivery: BroadcastDelivery,
    limiter: BroadcastRateLimiter,
    semaphore: asyncio.Semaphore,
) -> DeliveryResult:
    keyboard = broadcast_purchase_keyboard()

    async def operation() -> object:
        return await bot.copy_message(
            chat_id=delivery.user_id,
            from_chat_id=payload.source_chat_id,
            message_id=payload.source_message_id,
            reply_markup=keyboard,
        )

    delivered, inactive, error = await _send_with_retry(
        operation,
        limiter,
        semaphore,
    )
    return DeliveryResult(
        delivery_id=delivery.id,
        user_id=delivery.user_id,
        delivered=delivered,
        inactive=inactive,
        error=error,
    )


async def _save_delivery_result(
    session_factory: async_sessionmaker[AsyncSession],
    result: DeliveryResult,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            delivery = await session.get(BroadcastDelivery, result.delivery_id)
            if delivery is None or delivery.status != "sending":
                return
            delivery.status = "sent" if result.delivered else "failed"
            delivery.last_error = result.error
            delivery.sent_at = datetime.now(UTC) if result.delivered else None
            if result.inactive:
                await session.execute(
                    update(User)
                    .where(User.telegram_id == result.user_id)
                    .values(has_started=False)
                )


async def _sync_broadcast_progress(
    session_factory: async_sessionmaker[AsyncSession],
    broadcast_id: int,
) -> bool:
    async with session_factory() as session:
        async with session.begin():
            campaign = await session.scalar(
                select(BroadcastLog)
                .where(BroadcastLog.id == broadcast_id)
                .with_for_update()
            )
            if campaign is None:
                return True
            sent, failed, unfinished = (
                await session.execute(
                    select(
                        func.count(BroadcastDelivery.id).filter(
                            BroadcastDelivery.status == "sent"
                        ),
                        func.count(BroadcastDelivery.id).filter(
                            BroadcastDelivery.status == "failed"
                        ),
                        func.count(BroadcastDelivery.id).filter(
                            BroadcastDelivery.status.in_(("pending", "sending"))
                        ),
                    ).where(BroadcastDelivery.broadcast_id == broadcast_id)
                )
            ).one()
            campaign.delivered_count = int(sent)
            campaign.failed_count = int(failed)
            if int(unfinished) == 0:
                campaign.status = "completed"
                campaign.completed_at = datetime.now(UTC)
                return True
            return False


async def deliver_queued_broadcasts(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    limiter: BroadcastRateLimiter,
    *,
    concurrency: int = 12,
    batch_size: int = 100,
    campaign_limit: int = 1,
) -> int:
    processed_campaigns = 0
    semaphore = asyncio.Semaphore(max(1, concurrency))
    while processed_campaigns < campaign_limit:
        payload = await _claim_broadcast(session_factory)
        if payload is None:
            break
        while True:
            deliveries = await _claim_delivery_batch(
                session_factory,
                payload.broadcast_id,
                max(1, batch_size),
            )
            if not deliveries:
                await _sync_broadcast_progress(session_factory, payload.broadcast_id)
                break
            tasks = [
                asyncio.create_task(
                    _send_queued_delivery(
                        bot,
                        payload,
                        delivery,
                        limiter,
                        semaphore,
                    )
                )
                for delivery in deliveries
            ]
            try:
                for completed in asyncio.as_completed(tasks):
                    result = await completed
                    await _save_delivery_result(session_factory, result)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            if await _sync_broadcast_progress(
                session_factory,
                payload.broadcast_id,
            ):
                break
        processed_campaigns += 1
    return processed_campaigns


async def broadcast_worker(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    *,
    rate_per_second: int = 20,
    concurrency: int = 12,
    batch_size: int = 100,
    poll_seconds: float = 1.0,
) -> None:
    limiter = BroadcastRateLimiter(rate_per_second)
    await recover_interrupted_broadcasts(session_factory)
    while True:
        try:
            processed = await deliver_queued_broadcasts(
                session_factory,
                bot,
                limiter,
                concurrency=concurrency,
                batch_size=batch_size,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Could not deliver queued broadcasts")
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(BroadcastLog)
                        .where(BroadcastLog.status == "sending")
                        .values(last_error=str(exc)[:500])
                    )
            await recover_interrupted_broadcasts(session_factory)
            processed = 0
        if processed == 0:
            await asyncio.sleep(max(0.25, poll_seconds))


async def _claim_sale_alert(
    session_factory: async_sessionmaker[AsyncSession],
) -> SaleAlertPayload | None:
    now = datetime.now(UTC)
    recovery_cutoff = now - timedelta(minutes=15)
    async with session_factory() as session:
        await session.execute(
            update(ProductPriceAlert)
            .where(
                ProductPriceAlert.status == "sending",
                ProductPriceAlert.sent_at < recovery_cutoff,
            )
            .values(status="pending", sent_at=None)
        )
        rows = list(
            (
                await session.execute(
                    select(ProductPriceAlert, Product)
                    .join(Product, Product.id == ProductPriceAlert.product_id)
                    .where(ProductPriceAlert.status == "pending")
                    .order_by(ProductPriceAlert.id)
                    .limit(50)
                )
            ).all()
        )
        for alert, product in rows:
            if (
                not product.active
                or product.price != alert.sale_price_after
                or product.fulfillment_source != alert.provider
            ):
                alert.status = "superseded"
                continue
            if product.external_stock <= 0:
                continue

            recipients = tuple(
                (
                    await session.execute(
                        select(User.telegram_id, User.language)
                        .where(User.has_started.is_(True))
                        .order_by(User.telegram_id)
                    )
                ).all()
            )
            alert.status = "sending"
            alert.sent_at = now
            payload = SaleAlertPayload(
                alert_id=alert.id,
                product_id=product.id,
                name_vi=product.name_vi,
                name_en=product.name_en,
                old_price=alert.sale_price_before,
                new_price=alert.sale_price_after,
                stock=product.external_stock,
                recipients=recipients,
            )
            await session.commit()
            return payload

        await session.commit()
    return None


async def _send_sale_alert(
    bot: Bot,
    payload: SaleAlertPayload,
    user_id: int,
    language: str,
) -> bool:
    normalized_language = "en" if language == "en" else "vi"
    try:
        await bot.send_message(
            user_id,
            sale_alert_text(payload, normalized_language),
            reply_markup=sale_purchase_keyboard(
                payload.product_id,
                payload.new_price,
                normalized_language,
            ),
        )
    except TelegramRetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after) + 0.2)
        await bot.send_message(
            user_id,
            sale_alert_text(payload, normalized_language),
            reply_markup=sale_purchase_keyboard(
                payload.product_id,
                payload.new_price,
                normalized_language,
            ),
        )
    return True


async def deliver_pending_sale_alerts(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    *,
    throttle_seconds: float = 0.05,
    batch_limit: int = 5,
) -> int:
    processed = 0
    while processed < batch_limit:
        payload = await _claim_sale_alert(session_factory)
        if payload is None:
            break

        delivered = 0
        failed = 0
        inactive_ids: list[int] = []
        for user_id, language in payload.recipients:
            try:
                await _send_sale_alert(bot, payload, user_id, language)
            except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter) as exc:
                failed += 1
                if is_unreachable_error(exc):
                    inactive_ids.append(user_id)
            except Exception:
                failed += 1
                logger.exception("Unexpected automatic sale alert failure for user %s", user_id)
            else:
                delivered += 1
            if throttle_seconds > 0:
                await asyncio.sleep(throttle_seconds)

        async with session_factory() as session:
            if inactive_ids:
                await session.execute(
                    update(User)
                    .where(User.telegram_id.in_(inactive_ids))
                    .values(has_started=False)
                )
            await session.execute(
                update(ProductPriceAlert)
                .where(
                    ProductPriceAlert.id == payload.alert_id,
                    or_(
                        ProductPriceAlert.status == "sending",
                        ProductPriceAlert.status == "pending",
                    ),
                )
                .values(
                    status="sent",
                    total_recipients=len(payload.recipients),
                    delivered_count=delivered,
                    failed_count=failed,
                    sent_at=datetime.now(UTC),
                )
            )
            await session.commit()
        processed += 1
    return processed


async def _claim_stock_alert(
    session_factory: async_sessionmaker[AsyncSession],
) -> StockAlertPayload | None:
    now = datetime.now(UTC)
    recovery_cutoff = now - timedelta(minutes=15)
    async with session_factory() as session:
        await session.execute(
            update(ProductStockAlert)
            .where(
                ProductStockAlert.status == "sending",
                ProductStockAlert.sent_at < recovery_cutoff,
            )
            .values(status="pending", sent_at=None)
        )
        rows = list(
            (
                await session.execute(
                    select(ProductStockAlert, Product)
                    .join(Product, Product.id == ProductStockAlert.product_id)
                    .where(ProductStockAlert.status == "pending")
                    .order_by(ProductStockAlert.id)
                    .limit(50)
                )
            ).all()
        )
        for alert, product in rows:
            if (
                not product.active
                or product.fulfillment_source != alert.provider
                or product.external_stock <= 0
                or not stock_alert_enabled(product)
            ):
                alert.status = "superseded"
                continue

            recipients = tuple(
                (
                    await session.execute(
                        select(User.telegram_id, User.language)
                        .where(User.has_started.is_(True))
                        .order_by(User.telegram_id)
                    )
                ).all()
            )
            alert.status = "sending"
            alert.stock_after = product.external_stock
            alert.sale_price = product.price
            alert.sent_at = now
            payload = StockAlertPayload(
                alert_id=alert.id,
                product_id=product.id,
                name_vi=product.name_vi,
                name_en=product.name_en,
                price=product.price,
                stock=product.external_stock,
                recipients=recipients,
            )
            # Snapshot the outgoing content so the admin history remains an
            # accurate audit trail even after the product name or price changes.
            alert.message_vi = stock_alert_text(payload, "vi")
            alert.message_en = stock_alert_text(payload, "en")
            await session.commit()
            return payload

        await session.commit()
    return None


async def backfill_stock_alert_messages(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Populate message snapshots for alerts created before history support."""
    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(ProductStockAlert, Product)
                    .join(Product, Product.id == ProductStockAlert.product_id)
                    .where(
                        or_(
                            ProductStockAlert.message_vi.is_(None),
                            ProductStockAlert.message_en.is_(None),
                        )
                    )
                )
            ).all()
        )
        for alert, product in rows:
            payload = StockAlertPayload(
                alert_id=alert.id,
                product_id=product.id,
                name_vi=product.name_vi,
                name_en=product.name_en,
                price=alert.sale_price,
                stock=alert.stock_after,
                recipients=(),
            )
            if alert.message_vi is None:
                alert.message_vi = stock_alert_text(payload, "vi")
            if alert.message_en is None:
                alert.message_en = stock_alert_text(payload, "en")
        if rows:
            await session.commit()


async def _send_stock_alert(
    bot: Bot,
    payload: StockAlertPayload,
    user_id: int,
    language: str,
) -> bool:
    normalized_language = "en" if language == "en" else "vi"
    try:
        await bot.send_message(
            user_id,
            stock_alert_text(payload, normalized_language),
            reply_markup=sale_purchase_keyboard(
                payload.product_id,
                payload.price,
                normalized_language,
            ),
        )
    except TelegramRetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after) + 0.2)
        await bot.send_message(
            user_id,
            stock_alert_text(payload, normalized_language),
            reply_markup=sale_purchase_keyboard(
                payload.product_id,
                payload.price,
                normalized_language,
            ),
        )
    return True


async def deliver_pending_stock_alerts(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    *,
    throttle_seconds: float = 0.05,
    batch_limit: int = 5,
) -> int:
    processed = 0
    while processed < batch_limit:
        payload = await _claim_stock_alert(session_factory)
        if payload is None:
            break

        delivered = 0
        failed = 0
        inactive_ids: list[int] = []
        for user_id, language in payload.recipients:
            try:
                await _send_stock_alert(bot, payload, user_id, language)
            except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter) as exc:
                failed += 1
                if is_unreachable_error(exc):
                    inactive_ids.append(user_id)
            except Exception:
                failed += 1
                logger.exception("Unexpected back-in-stock alert failure for user %s", user_id)
            else:
                delivered += 1
            if throttle_seconds > 0:
                await asyncio.sleep(throttle_seconds)

        async with session_factory() as session:
            if inactive_ids:
                await session.execute(
                    update(User)
                    .where(User.telegram_id.in_(inactive_ids))
                    .values(has_started=False)
                )
            await session.execute(
                update(ProductStockAlert)
                .where(
                    ProductStockAlert.id == payload.alert_id,
                    or_(
                        ProductStockAlert.status == "sending",
                        ProductStockAlert.status == "pending",
                    ),
                )
                .values(
                    status="sent",
                    total_recipients=len(payload.recipients),
                    delivered_count=delivered,
                    failed_count=failed,
                    sent_at=datetime.now(UTC),
                )
            )
            await session.commit()
        processed += 1
    return processed


async def sale_alert_worker(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    poll_seconds: int = 5,
) -> None:
    while True:
        try:
            await deliver_pending_sale_alerts(session_factory, bot)
            await deliver_pending_stock_alerts(session_factory, bot)
        except Exception:
            logger.exception("Could not deliver automatic supplier product alerts")
        await asyncio.sleep(max(2, poll_seconds))
