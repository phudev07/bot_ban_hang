import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape

from aiogram import Bot
from sqlalchemy import exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased, selectinload

from app.delivery import delivery_files, delivery_keyboard, delivery_text
from app.haji_suppliers import HajiClient
from app.lehai_suppliers import LeHaiPremiumClient
from app.models import InventoryItem, Order, Preorder, Product, User, WalletTransaction
from app.product_tutorials import send_purchase_tutorials
from app.services import active_products, purchase_product
from app.supplier_recovery import queue_supplier_recovery
from app.suppliers import EXTERNAL_FULFILLMENT_SOURCES, ExternalSupplierClient, SumistoreClient
from app.utils import SecretCipher, format_vnd, sanitize_customer_text
from app.wallet_ledger import apply_wallet_change


logger = logging.getLogger(__name__)
ACTIVE_PREORDER_STATUSES = ("pending", "processing")
TERMINAL_PREORDER_STATUSES = ("completed", "cancelled")
PREORDER_SURCHARGE_PERCENT = 5
STALE_PROCESSING_SECONDS = 300


class PreorderError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PreorderQuote:
    base_unit_price: int
    preorder_unit_price: int
    quantity: int
    total_amount: int


def preorder_unit_price(base_unit_price: int) -> int:
    base_price = max(0, int(base_unit_price))
    return (base_price * (100 + PREORDER_SURCHARGE_PERCENT) + 99) // 100


def preorder_quote(product: Product, quantity: int) -> PreorderQuote:
    unit_price = preorder_unit_price(product.price)
    return PreorderQuote(
        base_unit_price=int(product.price),
        preorder_unit_price=unit_price,
        quantity=int(quantity),
        total_amount=unit_price * int(quantity),
    )


async def _cached_stock(session: AsyncSession, product: Product) -> int:
    local_stock = int(
        await session.scalar(
            select(func.count(InventoryItem.id)).where(
                InventoryItem.product_id == product.id,
                InventoryItem.status == "available",
            )
        )
        or 0
    )
    if product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES:
        supplier_stock = max(0, int(product.external_stock) - local_stock)
        # A purchase uses local stock only when it can satisfy the full order;
        # otherwise the full quantity is sourced through enabled API routes.
        return max(local_stock, supplier_stock)
    return local_stock


async def preorderable_products(session: AsyncSession) -> list[Product]:
    products = await active_products(session)
    return [
        product
        for product in products
        if not product.force_out_of_stock
        and int(getattr(product, "_menu_stock", 0)) <= 0
        and int(product.price) > 0
    ]


async def create_preorder(
    session: AsyncSession,
    user_id: int,
    product_id: int,
    quantity: int,
    *,
    expected_base_unit_price: int,
    max_active_per_user: int,
) -> Preorder:
    user = await session.scalar(
        select(User).where(User.telegram_id == user_id).with_for_update()
    )
    product = await session.scalar(
        select(Product).where(Product.id == product_id).with_for_update()
    )
    if user is None or product is None:
        raise PreorderError("not_found")
    if user.is_blocked:
        raise PreorderError("blocked")
    if (
        not product.active
        or product.archived_at is not None
        or product.product_type != "account"
        or product.force_out_of_stock
    ):
        raise PreorderError("not_available")
    if int(product.price) != int(expected_base_unit_price):
        raise PreorderError("price_changed")
    if quantity < 1 or quantity > max(1, int(product.max_quantity)):
        raise PreorderError("invalid_quantity")
    if quantity > 1 and not product.allow_quantity:
        raise PreorderError("invalid_quantity")
    if await _cached_stock(session, product) > 0:
        raise PreorderError("in_stock")

    active_for_product = await session.scalar(
        select(Preorder.id)
        .where(
            Preorder.user_id == user_id,
            Preorder.product_id == product_id,
            Preorder.status.in_(ACTIVE_PREORDER_STATUSES),
        )
        .limit(1)
    )
    if active_for_product is not None:
        raise PreorderError("duplicate")
    active_count = int(
        await session.scalar(
            select(func.count(Preorder.id)).where(
                Preorder.user_id == user_id,
                Preorder.status.in_(ACTIVE_PREORDER_STATUSES),
            )
        )
        or 0
    )
    if active_count >= max(1, int(max_active_per_user)):
        raise PreorderError("active_limit")

    quote = preorder_quote(product, quantity)
    if int(user.balance) < quote.total_amount:
        raise PreorderError("insufficient")
    preorder = Preorder(
        user_id=user_id,
        product_id=product.id,
        product_name_vi=product.name_vi,
        product_name_en=product.name_en,
        quantity=quantity,
        base_unit_price=quote.base_unit_price,
        preorder_unit_price=quote.preorder_unit_price,
        total_amount=quote.total_amount,
        status="pending",
        next_attempt_at=datetime.now(UTC),
        notification_status="none",
    )
    session.add(preorder)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise PreorderError("duplicate") from exc
    apply_wallet_change(
        session,
        user,
        -quote.total_amount,
        kind="preorder_charge",
        event_key=f"preorder_charge:{preorder.id}",
        reference_type="preorder",
        reference_id=preorder.code,
        description=(
            f"Đặt trước {quantity} tài khoản {product.name_vi} · giá đã gồm 5%"
        ),
    )
    preorder.funds_charged = True
    return preorder


async def recent_user_preorders(
    session: AsyncSession,
    user_id: int,
    *,
    limit: int = 30,
) -> list[Preorder]:
    return list(
        await session.scalars(
            select(Preorder)
            .where(Preorder.user_id == user_id)
            .order_by(Preorder.id.desc())
            .limit(max(1, min(100, int(limit))))
        )
    )


async def user_preorder(
    session: AsyncSession,
    user_id: int,
    preorder_id: int,
) -> Preorder | None:
    return await session.scalar(
        select(Preorder).where(
            Preorder.id == preorder_id,
            Preorder.user_id == user_id,
        )
    )


async def cancel_user_preorder(
    session: AsyncSession,
    user_id: int,
    preorder_id: int,
) -> Preorder:
    preorder = await session.scalar(
        select(Preorder)
        .where(
            Preorder.id == preorder_id,
            Preorder.user_id == user_id,
        )
        .with_for_update()
    )
    if preorder is None:
        raise PreorderError("not_found")
    if preorder.status != "pending":
        raise PreorderError("too_late")
    await _cancel_and_refund_locked(
        session,
        preorder,
        reason="user_cancelled",
        cancelled_by=f"user:{user_id}",
        notify=True,
    )
    await session.flush()
    return preorder


async def admin_cancel_preorder(
    session: AsyncSession,
    preorder_id: int,
    *,
    reason: str,
    admin_username: str,
) -> Preorder:
    clean_reason = " ".join(reason.split()).strip()
    if not clean_reason:
        raise PreorderError("cancel_reason_required")
    preorder = await session.scalar(
        select(Preorder).where(Preorder.id == preorder_id).with_for_update()
    )
    if preorder is None:
        raise PreorderError("not_found")
    if preorder.status != "pending":
        raise PreorderError("too_late")
    await _cancel_and_refund_locked(
        session,
        preorder,
        reason="admin_cancelled",
        cancel_note=clean_reason[:500],
        cancelled_by=f"admin:{admin_username}"[:255],
        notify=True,
    )
    await session.flush()
    return preorder


async def _cancel_and_refund_locked(
    session: AsyncSession,
    preorder: Preorder,
    *,
    reason: str,
    cancelled_by: str,
    notify: bool,
    cancel_note: str | None = None,
) -> None:
    user = await session.scalar(
        select(User).where(User.telegram_id == preorder.user_id).with_for_update()
    )
    if user is None:
        raise PreorderError("not_found")
    refund_event_key = f"preorder_refund:{preorder.id}"
    existing_refund = await session.scalar(
        select(WalletTransaction.id).where(
            WalletTransaction.event_key == refund_event_key
        )
    )
    if preorder.funds_charged and preorder.refunded_at is None:
        if existing_refund is None:
            apply_wallet_change(
                session,
                user,
                preorder.total_amount,
                kind="preorder_refund",
                event_key=refund_event_key,
                reference_type="preorder",
                reference_id=preorder.code,
                description=(
                    f"Hoàn tiền đơn đặt trước {preorder.code} · "
                    f"{cancel_note or reason}"
                ),
            )
        preorder.refunded_at = datetime.now(UTC)
    preorder.status = "cancelled"
    preorder.cancel_reason = reason[:64]
    preorder.cancel_note = cancel_note
    preorder.cancelled_by = cancelled_by[:255]
    preorder.cancelled_at = datetime.now(UTC)
    preorder.processing_started_at = None
    preorder.next_attempt_at = datetime.now(UTC) if notify else None
    preorder.notification_status = "pending" if notify else "none"


def preorder_detail_text(preorder: Preorder, language: str) -> str:
    name = sanitize_customer_text(
        preorder.product_name_en if language == "en" else preorder.product_name_vi
    )
    status = {
        "pending": "Đang chờ hàng",
        "processing": "Đang lấy và giao hàng",
        "completed": "Đã giao thành công",
        "cancelled": "Đã hủy",
    }.get(preorder.status, preorder.status)
    if language == "en":
        status = {
            "pending": "Waiting for stock",
            "processing": "Fulfilling",
            "completed": "Delivered",
            "cancelled": "Cancelled",
        }.get(preorder.status, preorder.status)
        reason_line = ""
        if preorder.status == "cancelled":
            reason = {
                "user_cancelled": "Cancelled by you",
                "admin_cancelled": preorder.cancel_note or "Cancelled by shop admin",
                "price_changed": "Product price changed",
                "insufficient_balance": "Insufficient wallet balance when stock returned",
                "product_unavailable": "Product is no longer available",
                "user_blocked": "Account is restricted",
            }.get(preorder.cancel_reason or "", "Could not complete the preorder")
            reason_line = f"• Reason: <b>{escape(reason)}</b>\n"
        payment_status = (
            f"Refunded <b>{format_vnd(preorder.total_amount)}</b> to wallet"
            if preorder.refunded_at is not None
            else f"Paid <b>{format_vnd(preorder.total_amount)}</b>"
            if preorder.funds_charged
            else "Charged on delivery (legacy preorder)"
        )
        return (
            f"📦 <b>Preorder {preorder.code}</b>\n\n"
            f"• Product: <b>{escape(name)}</b>\n"
            f"• Quantity: <b>{preorder.quantity}</b>\n"
            f"• Normal price at booking: <b>{format_vnd(preorder.base_unit_price)}/1</b>\n"
            f"• Preorder price (+5%): <b>{format_vnd(preorder.preorder_unit_price)}/1</b>\n"
            f"• Expected total: <b>{format_vnd(preorder.total_amount)}</b>\n"
            f"• Payment: {payment_status}\n"
            f"• Status: <b>{status}</b>\n\n"
            f"{reason_line}"
            "Cancelled preorders are refunded automatically."
        )
    reason_line = ""
    if preorder.status == "cancelled":
        reason = {
            "user_cancelled": "Bạn chủ động hủy",
            "admin_cancelled": preorder.cancel_note or "Admin hủy đơn",
            "price_changed": "Giá sản phẩm đã thay đổi",
            "insufficient_balance": "Ví không đủ tiền khi hàng về",
            "product_unavailable": "Sản phẩm không còn khả dụng",
            "user_blocked": "Tài khoản đang bị hạn chế",
        }.get(preorder.cancel_reason or "", "Không thể hoàn tất đơn đặt trước")
        reason_line = f"• Lý do: <b>{escape(reason)}</b>\n"
    payment_status = (
        f"Đã hoàn <b>{format_vnd(preorder.total_amount)}</b> về ví"
        if preorder.refunded_at is not None
        else f"Đã thanh toán <b>{format_vnd(preorder.total_amount)}</b>"
        if preorder.funds_charged
        else "Trừ tiền khi giao (đơn cũ)"
    )
    return (
        f"📦 <b>Đơn đặt trước {preorder.code}</b>\n\n"
        f"• Sản phẩm: <b>{escape(name)}</b>\n"
        f"• Số lượng: <b>{preorder.quantity}</b>\n"
        f"• Giá thường lúc đặt: <b>{format_vnd(preorder.base_unit_price)}/1</b>\n"
        f"• Giá đặt trước (+5%): <b>{format_vnd(preorder.preorder_unit_price)}/1</b>\n"
        f"• Tổng dự kiến: <b>{format_vnd(preorder.total_amount)}</b>\n"
        f"• Thanh toán: {payment_status}\n"
        f"• Trạng thái: <b>{status}</b>\n\n"
        f"{reason_line}"
        "Mọi đơn bị hủy đều được tự động hoàn tiền về ví."
    )


async def _recover_stale_preorders(session_factory: async_sessionmaker[AsyncSession]) -> None:
    cutoff = datetime.now(UTC) - timedelta(seconds=STALE_PROCESSING_SECONDS)
    async with session_factory() as session:
        stale = list(
            await session.scalars(
                select(Preorder)
                .where(
                    Preorder.status == "processing",
                    Preorder.processing_started_at < cutoff,
                )
                .with_for_update(skip_locked=True)
                .limit(20)
            )
        )
        for preorder in stale:
            existing_order = await session.scalar(
                select(Order)
                .where(Order.preorder_id == preorder.id)
                .order_by(Order.id)
                .limit(1)
            )
            if existing_order is not None:
                preorder.status = "completed"
                preorder.completed_order_code = existing_order.shop_order_code
                preorder.completed_at = existing_order.delivered_at or datetime.now(UTC)
                preorder.funds_charged = True
                preorder.processing_started_at = None
                preorder.next_attempt_at = datetime.now(UTC)
                preorder.notification_status = "pending"
                preorder.last_error = "completed_after_restart"
                continue
            product = await session.get(Product, preorder.product_id)
            if (
                preorder.last_error == "supplier_call_started"
                and product is not None
                and product.fulfillment_source == "sumistore"
                and product.supplier_product_id
                and preorder.processing_started_at is not None
            ):
                await queue_supplier_recovery(
                    session,
                    provider="sumistore",
                    supplier_product_id=product.supplier_product_id,
                    quantity=preorder.quantity,
                    request_key=f"preorder-{preorder.id}",
                    started_at=preorder.processing_started_at,
                    error_code="PREORDER_WORKER_RESTARTED",
                    expires_after=timedelta(minutes=10),
                )
            preorder.status = "pending"
            preorder.processing_started_at = None
            preorder.next_attempt_at = datetime.now(UTC)
            preorder.last_error = "stale_processing_recovered"

        stale_notifications = list(
            await session.scalars(
                select(Preorder)
                .where(
                    Preorder.notification_status == "sending",
                    Preorder.updated_at < cutoff,
                )
                .with_for_update(skip_locked=True)
                .limit(20)
            )
        )
        for preorder in stale_notifications:
            preorder.notification_status = "failed"
            preorder.next_attempt_at = datetime.now(UTC)
        if stale or stale_notifications:
            await session.commit()


async def _claim_next_preorder(
    session_factory: async_sessionmaker[AsyncSession],
) -> Preorder | None:
    now = datetime.now(UTC)
    earlier = aliased(Preorder)
    async with session_factory() as session:
        preorder = await session.scalar(
            select(Preorder)
            .where(
                Preorder.status == "pending",
                or_(Preorder.next_attempt_at.is_(None), Preorder.next_attempt_at <= now),
                ~exists(
                    select(earlier.id).where(
                        earlier.product_id == Preorder.product_id,
                        earlier.status.in_(ACTIVE_PREORDER_STATUSES),
                        or_(
                            earlier.created_at < Preorder.created_at,
                            (
                                (earlier.created_at == Preorder.created_at)
                                & (earlier.id < Preorder.id)
                            ),
                        ),
                    )
                ),
            )
            .order_by(Preorder.created_at, Preorder.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if preorder is None:
            return None
        preorder.status = "processing"
        preorder.processing_started_at = now
        preorder.attempt_count += 1
        preorder.last_error = None
        await session.commit()
        return preorder


async def _set_pending_retry(
    session_factory: async_sessionmaker[AsyncSession],
    preorder_id: int,
    error: str,
    *,
    delay_seconds: int,
) -> None:
    async with session_factory() as session:
        preorder = await session.scalar(
            select(Preorder).where(Preorder.id == preorder_id).with_for_update()
        )
        if preorder is None or preorder.status != "processing":
            return
        preorder.status = "pending"
        preorder.processing_started_at = None
        preorder.last_error = error[:64]
        preorder.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        await session.commit()


async def _cancel_preorder(
    session_factory: async_sessionmaker[AsyncSession],
    preorder_id: int,
    reason: str,
) -> None:
    async with session_factory() as session:
        preorder = await session.scalar(
            select(Preorder).where(Preorder.id == preorder_id).with_for_update()
        )
        if preorder is None or preorder.status not in ACTIVE_PREORDER_STATUSES:
            return
        await _cancel_and_refund_locked(
            session,
            preorder,
            reason=reason,
            cancelled_by="system:preorder_worker",
            notify=True,
        )
        await session.commit()


async def _complete_preorder(
    session_factory: async_sessionmaker[AsyncSession],
    preorder_id: int,
    order_code: str,
) -> None:
    async with session_factory() as session:
        preorder = await session.scalar(
            select(Preorder).where(Preorder.id == preorder_id).with_for_update()
        )
        if preorder is None or preorder.status == "cancelled":
            return
        preorder.status = "completed"
        preorder.completed_order_code = order_code
        preorder.completed_at = datetime.now(UTC)
        preorder.funds_charged = True
        preorder.processing_started_at = None
        preorder.next_attempt_at = datetime.now(UTC)
        preorder.notification_status = "pending"
        await session.commit()


async def _process_claimed_preorder(
    session_factory: async_sessionmaker[AsyncSession],
    preorder: Preorder,
    cipher: SecretCipher,
    supplier_client: SumistoreClient | None,
    lehai_client: LeHaiPremiumClient | None,
    canboso_client: ExternalSupplierClient | None,
    nce_client: ExternalSupplierClient | None,
    haji_client: HajiClient | None,
    referral_commission_percent: int,
) -> None:
    async with session_factory() as session:
        current = await session.get(Preorder, preorder.id)
        if current is None or current.status != "processing":
            return
        product = await session.get(Product, current.product_id)
        user = await session.get(User, current.user_id)
        if (
            product is None
            or user is None
            or not product.active
            or product.archived_at is not None
            or product.force_out_of_stock
        ):
            await session.commit()
            await _cancel_preorder(session_factory, current.id, "product_unavailable")
            return
        if int(product.price) != int(current.base_unit_price):
            await session.commit()
            await _cancel_preorder(session_factory, current.id, "price_changed")
            return
        if user.is_blocked:
            await session.commit()
            await _cancel_preorder(session_factory, current.id, "user_blocked")
            return
        if not current.funds_charged and int(user.balance) < int(current.total_amount):
            await session.commit()
            await _cancel_preorder(session_factory, current.id, "insufficient_balance")
            return
        stock = await _cached_stock(session, product)
        uses_supplier_api = product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES
        if uses_supplier_api:
            current.last_error = "supplier_call_started"
        await session.commit()
        if stock < current.quantity and not uses_supplier_api:
            await _set_pending_retry(
                session_factory,
                current.id,
                "waiting_for_stock",
                delay_seconds=2,
            )
            return

    result = await purchase_product(
        session_factory,
        preorder.user_id,
        preorder.product_id,
        cipher,
        preorder.quantity,
        supplier_client,
        lehai_client=lehai_client,
        canboso_client=canboso_client,
        nce_client=nce_client,
        haji_client=haji_client,
        sales_channel="preorder",
        referral_commission_percent=referral_commission_percent,
        supplier_idempotency_key=f"preorder-{preorder.id}",
        preorder_id=preorder.id,
        expected_base_unit_price=preorder.base_unit_price,
        fixed_unit_price=preorder.preorder_unit_price,
        wallet_already_charged=preorder.funds_charged,
    )
    if result.ok and result.orders:
        await _complete_preorder(
            session_factory,
            preorder.id,
            result.orders[0].shop_order_code,
        )
        return
    if result.message == "price_changed":
        await _cancel_preorder(session_factory, preorder.id, "price_changed")
        return
    if result.message == "insufficient":
        await _cancel_preorder(session_factory, preorder.id, "insufficient_balance")
        return
    if result.message in {
        "not_found",
        "blocked",
        "invalid_quantity",
        "invalid_price",
        "preorder_unavailable",
    }:
        await _cancel_preorder(session_factory, preorder.id, result.message)
        return
    if result.message == "out_of_stock":
        await _set_pending_retry(
            session_factory,
            preorder.id,
            result.message,
            delay_seconds=5,
        )
        return
    retry_delay = min(300, max(20, 15 * (2 ** min(preorder.attempt_count, 4))))
    await _set_pending_retry(
        session_factory,
        preorder.id,
        result.message,
        delay_seconds=retry_delay if result.message == "supplier_unavailable" else 5,
    )


def _cancel_notification_text(preorder: Preorder, user: User) -> str:
    language = user.language
    name = sanitize_customer_text(
        preorder.product_name_en if language == "en" else preorder.product_name_vi
    )
    if preorder.cancel_reason == "admin_cancelled" and preorder.cancel_note:
        reason = preorder.cancel_note
    elif preorder.cancel_reason == "user_cancelled":
        reason = (
            "You cancelled this preorder."
            if language == "en"
            else "Bạn đã chủ động hủy đơn đặt trước này."
        )
    elif preorder.cancel_reason == "price_changed":
        reason = (
            "The product price changed before stock returned."
            if language == "en"
            else "Giá sản phẩm đã thay đổi trước khi có hàng."
        )
    elif preorder.cancel_reason == "insufficient_balance":
        reason = (
            "Your wallet balance was insufficient when stock returned."
            if language == "en"
            else "Số dư ví không đủ tại thời điểm hàng về."
        )
    else:
        reason = (
            "The product was no longer available for preorder."
            if language == "en"
            else "Sản phẩm không còn đủ điều kiện giao đặt trước."
        )
    if language == "en":
        return (
            f"❌ <b>Preorder {preorder.code} cancelled</b>\n\n"
            f"• Product: <b>{escape(name)}</b>\n"
            f"• Quantity: <b>{preorder.quantity}</b>\n"
            f"• Reason: {escape(reason)}\n\n"
            f"Refunded to wallet: <b>{format_vnd(preorder.total_amount)}</b>."
        )
    return (
        f"❌ <b>Đã hủy đơn đặt trước {preorder.code}</b>\n\n"
        f"• Sản phẩm: <b>{escape(name)}</b>\n"
        f"• Số lượng: <b>{preorder.quantity}</b>\n"
        f"• Lý do: {escape(reason)}\n\n"
        f"Đã hoàn về ví: <b>{format_vnd(preorder.total_amount)}</b>."
    )


async def _claim_notification(
    session_factory: async_sessionmaker[AsyncSession],
) -> int | None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        preorder = await session.scalar(
            select(Preorder)
            .where(
                Preorder.status.in_(TERMINAL_PREORDER_STATUSES),
                Preorder.notification_status.in_(("pending", "failed")),
                or_(Preorder.next_attempt_at.is_(None), Preorder.next_attempt_at <= now),
            )
            .order_by(Preorder.updated_at, Preorder.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if preorder is None:
            return None
        preorder.notification_status = "sending"
        preorder.notification_attempt_count += 1
        await session.commit()
        return preorder.id


async def _send_preorder_notification(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    cipher: SecretCipher,
    preorder_id: int,
    codex_docs_url: str,
) -> None:
    async with session_factory() as session:
        preorder = await session.scalar(
            select(Preorder)
            .where(Preorder.id == preorder_id)
            .options(selectinload(Preorder.user), selectinload(Preorder.product))
        )
        if preorder is None or preorder.notification_status != "sending":
            return
        user = preorder.user
        orders: list[Order] = []
        if preorder.status == "completed":
            orders = list(
                await session.scalars(
                    select(Order)
                    .where(Order.preorder_id == preorder.id)
                    .options(selectinload(Order.inventory_item))
                    .order_by(Order.id)
                )
            )
        await session.commit()

    try:
        if preorder.status == "completed":
            if not orders:
                raise RuntimeError("completed_preorder_without_orders")
            order_ids = [order.id for order in orders]
            secrets = [
                cipher.decrypt(order.inventory_item.encrypted_secret) for order in orders
            ]
            product_name = sanitize_customer_text(
                preorder.product_name_en
                if user.language == "en"
                else preorder.product_name_vi
            )
            heading = (
                f"🎉 <b>Preorder {preorder.code} is ready</b>\n\n"
                if user.language == "en"
                else f"🎉 <b>Đơn đặt trước {preorder.code} đã có hàng</b>\n\n"
            )
            await bot.send_message(
                user.telegram_id,
                heading
                + delivery_text(
                    shop_order_code=orders[0].shop_order_code,
                    product_name=product_name,
                    secrets=secrets,
                    total_amount=sum(int(order.amount) for order in orders),
                    language=user.language,
                ),
                reply_markup=delivery_keyboard(
                    primary_order_id=min(order_ids),
                    secrets=secrets,
                    language=user.language,
                    include_file_button=orders[0].product_id != 28,
                    guide_url=(
                        codex_docs_url
                        if (
                            preorder.product
                            and (preorder.product.supplier_product_id or "").startswith(
                                "apicodex_"
                            )
                        )
                        else None
                    ),
                ),
            )
            if orders[0].product_id == 28:
                for document in delivery_files(
                    shop_order_code=orders[0].shop_order_code,
                    product_name=product_name,
                    secrets=secrets,
                    total_amount=sum(int(order.amount) for order in orders),
                    language=user.language,
                    product_id=orders[0].product_id,
                ):
                    await bot.send_document(user.telegram_id, document)
            await send_purchase_tutorials(
                bot,
                user.telegram_id,
                preorder.product.supplier_product_id if preorder.product else None,
                user.language,
                session_factory,
            )
        else:
            await bot.send_message(
                user.telegram_id,
                _cancel_notification_text(preorder, user),
            )
    except Exception as exc:
        logger.exception("Could not notify preorder %s", preorder_id)
        async with session_factory() as session:
            current = await session.get(Preorder, preorder_id)
            if current is not None and current.notification_status == "sending":
                current.notification_status = "failed"
                current.notification_last_error = str(exc)[:500]
                delay = min(900, 30 * (2 ** min(current.notification_attempt_count, 5)))
                current.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
                await session.commit()
        return

    async with session_factory() as session:
        current = await session.get(Preorder, preorder_id)
        if current is not None and current.notification_status == "sending":
            current.notification_status = "sent"
            current.notification_last_error = None
            current.notified_at = datetime.now(UTC)
            current.next_attempt_at = None
            await session.commit()


async def preorder_worker(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    cipher: SecretCipher,
    supplier_client: SumistoreClient | None,
    lehai_client: LeHaiPremiumClient | None,
    canboso_client: ExternalSupplierClient | None,
    nce_client: ExternalSupplierClient | None,
    haji_client: HajiClient | None,
    *,
    interval_seconds: int,
    referral_commission_percent: int,
    codex_docs_url: str = "",
) -> None:
    interval = max(2, int(interval_seconds))
    loop = asyncio.get_running_loop()
    next_recovery_at = 0.0
    while True:
        processed_preorders = 0
        processed_notifications = 0
        try:
            if loop.time() >= next_recovery_at:
                await _recover_stale_preorders(session_factory)
                next_recovery_at = loop.time() + 60
            for _ in range(20):
                preorder = await _claim_next_preorder(session_factory)
                if preorder is None:
                    break
                processed_preorders += 1
                await _process_claimed_preorder(
                    session_factory,
                    preorder,
                    cipher,
                    supplier_client,
                    lehai_client,
                    canboso_client,
                    nce_client,
                    haji_client,
                    referral_commission_percent,
                )
            for _ in range(20):
                notification_id = await _claim_notification(session_factory)
                if notification_id is None:
                    break
                processed_notifications += 1
                await _send_preorder_notification(
                    session_factory,
                    bot,
                    cipher,
                    notification_id,
                    codex_docs_url,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Preorder worker iteration failed")
        if processed_preorders >= 20 or processed_notifications >= 20:
            await asyncio.sleep(0)
            continue
        await asyncio.sleep(interval)
