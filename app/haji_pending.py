"""Durable reconciliation for Haji manual (Claude add-team) orders."""

import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.delivery import delivery_keyboard, delivery_text
from app.flash_sales import complete_deposit_flash_sale, release_deposit_flash_sale
from app.haji_suppliers import HajiClient
from app.models import (
    Deposit,
    InventoryItem,
    Order,
    PaymentTransaction,
    Product,
    SupplierBalanceTransaction,
    SupplierPurchaseAttempt,
    User,
    WalletTransaction,
)
from app.partner_services import award_referral_commission
from app.supplier_audit import record_supplier_purchase
from app.suppliers import SupplierError
from app.utils import SecretCipher, format_vnd
from app.price_alerts import release_price_lock_if_inventory_empty
from app.wallet_ledger import apply_wallet_change


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HajiPendingCompletion:
    user_id: int
    language: str
    product_id: int
    product_name_vi: str
    product_name_en: str
    shop_order_code: str
    amount: int
    order_ids: tuple[int, ...]
    secrets: tuple[str, ...]


@dataclass(frozen=True)
class HajiPendingFailure:
    """A supplier order that failed after payment was accepted."""

    user_id: int
    language: str
    amount: int
    balance: int
    deposit_code: str


def _is_terminal(status: str) -> bool:
    return status in {"done", "fulfilled", "success", "completed"}


async def settle_haji_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    client: HajiClient,
    cipher: SecretCipher,
    attempt_id: int,
    referral_commission_percent: int = 2,
) -> HajiPendingCompletion | HajiPendingFailure | None:
    async with session_factory() as read_session:
        attempt = await read_session.get(SupplierPurchaseAttempt, attempt_id)
        if (
            attempt is None
            or attempt.provider != "haji"
            or attempt.status != "processing"
            or not attempt.supplier_order_code
            or attempt.deposit_id is None
        ):
            return None
        order_code = attempt.supplier_order_code
        captured_unit_cost = int(attempt.unit_cost or 0)
        attempt_quantity = max(1, int(attempt.quantity or 1))

    try:
        remote = await client.check_order(order_code)
    except SupplierError as exc:
        logger.warning("Could not poll pending Haji order %s: %s", order_code, exc.code)
        return None
    if not _is_terminal(remote.status):
        if remote.status in {"failed", "cancelled", "canceled", "refunded"}:
            async with session_factory() as session:
                async with session.begin():
                    attempt = await session.scalar(
                        select(SupplierPurchaseAttempt)
                        .where(SupplierPurchaseAttempt.id == attempt_id)
                        .with_for_update()
                    )
                    if attempt is None or attempt.status != "processing":
                        return None
                    deposit = await session.scalar(
                        select(Deposit)
                        .where(Deposit.id == attempt.deposit_id)
                        .with_for_update()
                    )
                    if deposit is None:
                        attempt.status = "failed"
                        attempt.error_code = "SUPPLIER_ORDER_FAILED"
                        attempt.error_detail = remote.status
                        attempt.completed_at = datetime.now(UTC)
                        return None
                    user = await session.scalar(
                        select(User)
                        .where(User.telegram_id == deposit.user_id)
                        .with_for_update()
                    )
                    if user is None:
                        attempt.status = "failed"
                        attempt.error_code = "SUPPLIER_ORDER_FAILED"
                        attempt.error_detail = remote.status
                        attempt.completed_at = datetime.now(UTC)
                        return None

                    now = datetime.now(UTC)
                    # Keep the payment settled, but return the shop amount to
                    # the wallet exactly once when the supplier cannot fulfill.
                    refund_key = f"direct-purchase-refund:{deposit.id}"
                    refund_exists = await session.scalar(
                        select(WalletTransaction.id).where(
                            WalletTransaction.event_key == refund_key
                        )
                    )
                    if refund_exists is None:
                        apply_wallet_change(
                            session,
                            user,
                            int(deposit.requested_amount),
                            kind="direct_purchase_refund",
                            event_key=refund_key,
                            reference_type="deposit",
                            reference_id=deposit.code,
                            description=(
                                f"Hoàn tiền mua trực tiếp {deposit.code}: "
                                "nhà cung cấp không hoàn tất đơn Claude"
                            ),
                            currency=str(deposit.currency or "VND").upper(),
                        )
                    payment = await session.scalar(
                        select(PaymentTransaction)
                        .where(PaymentTransaction.deposit_id == deposit.id)
                        .order_by(PaymentTransaction.id.desc())
                        .with_for_update()
                    )
                    if payment is not None and payment.credit_status == "credited":
                        # This is an external payment record, not a wallet top-up.
                        # Keep it auditable while marking the supplier refund path.
                        payment.credit_status = "refunded"
                    await release_deposit_flash_sale(session, deposit)
                    deposit.failure_reason = "supplier_order_failed"
                    attempt.status = "failed"
                    attempt.error_code = "SUPPLIER_ORDER_FAILED"
                    attempt.error_detail = remote.status
                    attempt.completed_at = now
                    return HajiPendingFailure(
                        user_id=user.telegram_id,
                        language=user.language,
                        amount=int(deposit.requested_amount),
                        balance=int(user.balance),
                        deposit_code=deposit.code,
                    )
        return None
    if not remote.items or len(remote.items) != remote.quantity:
        return None
    # The supplier response is an add-team completion callback and may omit
    # pricing.  Cost must come from the quote captured when payment/order
    # submission happened, never from the live catalog (which may have
    # changed in the meantime).
    unit_price = captured_unit_cost
    if unit_price <= 0:
        # Backfill legacy attempts only from an already-recorded supplier debit
        # tied to this exact order.  This is historical data, not a catalog
        # lookup, and therefore cannot silently reprice an old customer order.
        async with session_factory() as history_session:
            debit = await history_session.scalar(
                select(SupplierBalanceTransaction)
                .where(
                    SupplierBalanceTransaction.provider == "haji",
                    SupplierBalanceTransaction.supplier_order_code == order_code,
                    SupplierBalanceTransaction.amount < 0,
                )
                .order_by(SupplierBalanceTransaction.id.desc())
            )
        if debit is not None:
            unit_price = abs(int(debit.amount)) // attempt_quantity
    if unit_price <= 0:
        logger.error(
            "Haji order %s completed without a captured historical unit cost; "
            "leaving it pending instead of using a live catalog price",
            order_code,
        )
        return None

    async with session_factory() as session:
        async with session.begin():
            attempt = await session.scalar(
                select(SupplierPurchaseAttempt)
                .where(SupplierPurchaseAttempt.id == attempt_id)
                .with_for_update()
            )
            if (
                attempt is None
                or attempt.status != "processing"
                or attempt.deposit_id is None
                or attempt.supplier_order_code != order_code
            ):
                return None
            if int(attempt.unit_cost or 0) <= 0:
                attempt.unit_cost = unit_price
            deposit = await session.scalar(
                select(Deposit).where(Deposit.id == attempt.deposit_id).with_for_update()
            )
            if deposit is None or deposit.status != "paid":
                return None
            user = await session.scalar(
                select(User).where(User.telegram_id == deposit.user_id).with_for_update()
            )
            product = await session.scalar(
                select(Product).where(Product.id == deposit.product_id).with_for_update()
            )
            if user is None or product is None:
                return None
            existing_item = await session.scalar(
                select(InventoryItem.id).where(
                    InventoryItem.supplier_order_code == order_code,
                    InventoryItem.supplier_item_index == 0,
                )
            )
            if existing_item is not None:
                attempt.status = "succeeded"
                attempt.error_code = None
                attempt.error_detail = None
                attempt.completed_at = attempt.completed_at or datetime.now(UTC)
                return None

            now = datetime.now(UTC)
            batch_code = f"B{secrets.token_hex(5).upper()}"
            item_rows: list[InventoryItem] = []
            orders: list[Order] = []
            quantity = len(remote.items)
            sale_unit = int(deposit.requested_amount) // quantity
            discount_unit = int(deposit.discount_amount or 0) // quantity
            for item_index, secret in enumerate(remote.items):
                item = InventoryItem(
                    product_id=product.id,
                    encrypted_secret=cipher.encrypt(secret),
                    account_fingerprint=cipher.inventory_fingerprint(secret),
                    cost_amount=unit_price,
                    supplier_order_code=order_code,
                    supplier_provider="haji",
                    supplier_item_index=item_index,
                    status="sold",
                    sold_at=now,
                )
                session.add(item)
                await session.flush()
                order = Order(
                    user_id=user.telegram_id,
                    product_id=product.id,
                    product_name_vi=product.name_vi,
                    product_name_en=product.name_en,
                    inventory_item_id=item.id,
                    amount=sale_unit,
                    cost_amount=unit_price,
                    discount_amount=discount_unit,
                    discount_code_id=deposit.discount_code_id,
                    discount_code=deposit.discount_code,
                    flash_sale_id=deposit.flash_sale_id,
                    seller_price_id=deposit.seller_price_id,
                    seller_profit_per_unit=deposit.seller_profit_per_unit,
                    batch_code=batch_code,
                    supplier_order_code=order_code,
                    supplier_provider="haji",
                    sales_channel="telegram",
                    status="completed",
                    delivered_at=now,
                )
                session.add(order)
                item_rows.append(item)
                orders.append(order)
            await session.flush()
            # Convert the matching unexplained debit into the purchase record.
            audit = await session.scalar(
                select(SupplierBalanceTransaction)
                .where(
                    SupplierBalanceTransaction.provider == "haji",
                    SupplierBalanceTransaction.kind == "suspicious",
                    SupplierBalanceTransaction.amount == -(unit_price * quantity),
                    SupplierBalanceTransaction.shop_order_code.is_(None),
                )
                .order_by(SupplierBalanceTransaction.id.desc())
                .with_for_update()
            )
            if audit is not None:
                audit.kind = "purchase"
                audit.supplier_order_code = order_code
                audit.shop_order_code = batch_code
                audit.product_id = product.id
                audit.quantity = quantity
                audit.note = "Đã đối soát đơn Haji Claude add-team hoàn tất sau timeout."
            else:
                record_supplier_purchase(
                    session,
                    amount=unit_price * quantity,
                    supplier_order_code=order_code,
                    shop_order_code=batch_code,
                    product_id=product.id,
                    quantity=quantity,
                    provider="haji",
                )
            product.external_stock = max(0, int(product.external_stock or 0) - quantity)
            await complete_deposit_flash_sale(session, deposit)
            await release_price_lock_if_inventory_empty(session, product)
            await award_referral_commission(
                session,
                user,
                shop_order_code=batch_code,
                order_amount=int(deposit.requested_amount),
                sales_channel="telegram",
                commission_percent=referral_commission_percent,
            )
            attempt.status = "succeeded"
            attempt.error_code = None
            attempt.error_detail = None
            attempt.completed_at = now
            return HajiPendingCompletion(
                user_id=user.telegram_id,
                language=user.language,
                product_id=product.id,
                product_name_vi=product.name_vi,
                product_name_en=product.name_en,
                shop_order_code=batch_code,
                amount=int(deposit.requested_amount),
                order_ids=tuple(order.id for order in orders),
                secrets=tuple(remote.items),
            )


async def haji_pending_worker(
    session_factory: async_sessionmaker[AsyncSession],
    client: HajiClient,
    cipher: SecretCipher,
    bot: Bot,
    interval_seconds: int = 10,
    referral_commission_percent: int = 2,
) -> None:
    while True:
        try:
            async with session_factory() as session:
                attempt_ids = list(
                    await session.scalars(
                        select(SupplierPurchaseAttempt.id)
                        .where(
                            SupplierPurchaseAttempt.provider == "haji",
                            SupplierPurchaseAttempt.status == "processing",
                            SupplierPurchaseAttempt.supplier_order_code.is_not(None),
                            SupplierPurchaseAttempt.deposit_id.is_not(None),
                        )
                        .order_by(SupplierPurchaseAttempt.id)
                        .limit(20)
                    )
                )
            for attempt_id in attempt_ids:
                completed = await settle_haji_attempt(
                    session_factory,
                    client,
                    cipher,
                    int(attempt_id),
                    referral_commission_percent,
                )
                if completed is None:
                    continue
                try:
                    if isinstance(completed, HajiPendingFailure):
                        await bot.send_message(
                            completed.user_id,
                            (
                                "⚠️ <b>Không thể hoàn tất đơn Claude</b>\n"
                                "Nhà cung cấp không hoàn tất việc thêm email vào team.\n"
                                f"Đã hoàn lại <b>{format_vnd(completed.amount)}</b> vào ví của bạn."
                                f"\nMã nạp: <code>{completed.deposit_code}</code>"
                                if completed.language == "vi"
                                else "⚠️ <b>Claude order could not be completed</b>\n"
                                "The supplier could not add your email to the team.\n"
                                f"<b>{format_vnd(completed.amount)}</b> was returned to your wallet."
                                f"\nDeposit code: <code>{completed.deposit_code}</code>"
                            ),
                        )
                    else:
                        await bot.send_message(
                            completed.user_id,
                            delivery_text(
                                shop_order_code=completed.shop_order_code,
                                product_name=(
                                    completed.product_name_en
                                    if completed.language == "en"
                                    else completed.product_name_vi
                                ),
                                secrets=list(completed.secrets),
                                total_amount=completed.amount,
                                language=completed.language,
                                paid_by_qr=True,
                            ),
                            reply_markup=delivery_keyboard(
                                primary_order_id=min(completed.order_ids),
                                secrets=list(completed.secrets),
                                language=completed.language,
                            ),
                        )
                except Exception:
                    logger.exception(
                        "Could not notify user %s about Haji Claude fulfillment",
                        completed.user_id,
                    )
        except Exception:
            logger.exception("Could not reconcile pending Haji manual orders")
        await asyncio.sleep(max(5, interval_seconds))
