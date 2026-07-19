import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import BroadcastLog, Product, ProductPriceAlert, ProductStockAlert, User
from app.utils import format_vnd, safe_html


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BroadcastResult:
    total: int
    delivered: int
    failed: int


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
            await session.commit()
            return payload

        await session.commit()
    return None


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
