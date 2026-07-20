import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from aiogram.types import User as TelegramUser
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.lehai_suppliers import LeHaiPremiumClient, refresh_lehai_product
from app.models import (
    Category,
    Deposit,
    DiscountCode,
    InventoryItem,
    Order,
    PaymentTransaction,
    Product,
    QuantityDiscount,
    SmsRental,
    SupplierBalanceTransaction,
    SupplierPurchaseAttempt,
    SupplierRecoveryRequest,
    User,
)
from app.price_alerts import apply_supplier_price
from app.partner_services import award_referral_commission, ensure_referral_code
from app.supplier_audit import record_supplier_purchase
from app.supplier_recovery import queue_supplier_recovery
from app.suppliers import (
    EXTERNAL_FULFILLMENT_SOURCES,
    SELLABLE_FULFILLMENT_SOURCES,
    ExternalSupplierClient,
    SumistoreClient,
    SupplierError,
    SupplierPurchase,
    refresh_external_product,
    supplier_balance_guard,
)
from app.utils import SecretCipher, find_deposit_code


logger = logging.getLogger(__name__)
RECOVERABLE_SUPPLIER_ERRORS = {
    "SUPPLIER_UNAVAILABLE",
    "SUPPLIER_INVALID_RESPONSE",
    "SUPPLIER_DELIVERY_INCOMPLETE",
}

FulfillmentStartedCallback = Callable[[int, str], Awaitable[None]]


async def notify_fulfillment_started(
    callback: FulfillmentStartedCallback | None,
    user_id: int,
    language: str,
) -> None:
    if callback is None:
        return
    try:
        await callback(user_id, language)
    except Exception:
        logger.exception("Could not notify user %s that fulfillment started", user_id)


async def reserve_available_inventory(
    session: AsyncSession,
    product_id: int,
    quantity: int,
) -> list[InventoryItem]:
    return list(
        await session.scalars(
            select(InventoryItem)
            .where(
                InventoryItem.product_id == product_id,
                InventoryItem.status == "available",
            )
            .order_by(InventoryItem.id)
            .with_for_update(skip_locked=True)
            .limit(quantity)
        )
    )


async def buy_supplier_product(
    session: AsyncSession,
    client: ExternalSupplierClient,
    product_id: str,
    quantity: int,
    *,
    idempotency_key: str | None = None,
) -> SupplierPurchase:
    provider = getattr(client, "provider", "sumistore")
    request_key = idempotency_key or f"shop-{secrets.token_hex(16)}"
    product_db_id = await session.scalar(
        select(Product.id).where(
            Product.fulfillment_source == provider,
            Product.supplier_product_id == product_id,
        )
    )
    attempt = await session.scalar(
        select(SupplierPurchaseAttempt).where(
            SupplierPurchaseAttempt.provider == provider,
            SupplierPurchaseAttempt.request_key == request_key,
        )
    )
    started_at = datetime.now(UTC)
    if attempt is None:
        attempt = SupplierPurchaseAttempt(
            provider=provider,
            request_key=request_key,
            product_id=product_db_id,
            supplier_product_id=product_id,
            quantity=quantity,
            status="processing",
            started_at=started_at,
        )
        session.add(attempt)
    else:
        attempt.product_id = product_db_id or attempt.product_id
        attempt.supplier_product_id = product_id
        attempt.quantity = quantity
        attempt.status = "processing"
        attempt.error_code = None
        attempt.error_detail = None
        attempt.started_at = started_at
        attempt.completed_at = None
    await session.flush()
    try:
        purchase = await _execute_supplier_purchase(
            session,
            client,
            product_id,
            quantity,
            idempotency_key=request_key,
        )
    except SupplierError as exc:
        attempt.status = "failed"
        attempt.error_code = exc.code
        attempt.error_detail = str(exc)[:500]
        attempt.completed_at = datetime.now(UTC)
        await session.flush()
        raise
    attempt.status = "succeeded"
    attempt.supplier_order_code = purchase.order_code or None
    attempt.completed_at = datetime.now(UTC)
    await session.flush()
    return purchase


async def _execute_supplier_purchase(
    session: AsyncSession,
    client: ExternalSupplierClient,
    product_id: str,
    quantity: int,
    *,
    idempotency_key: str | None = None,
) -> SupplierPurchase:
    provider = getattr(client, "provider", "sumistore")
    known_order_codes = set(
        await session.scalars(
            select(SupplierBalanceTransaction.supplier_order_code).where(
                SupplierBalanceTransaction.provider == provider,
                SupplierBalanceTransaction.supplier_order_code.is_not(None),
            )
        )
    )
    known_order_codes.update(
        await session.scalars(
            select(InventoryItem.supplier_order_code).where(
                InventoryItem.supplier_order_code.is_not(None)
            )
        )
    )
    known_order_codes.update(
        await session.scalars(
            select(SupplierRecoveryRequest.supplier_order_code).where(
                SupplierRecoveryRequest.provider == provider,
                SupplierRecoveryRequest.supplier_order_code.is_not(None),
            )
        )
    )
    started_at = datetime.now(UTC)
    try:
        if provider == "sumistore":
            return await client.buy(product_id, quantity)
        return await client.buy(product_id, quantity, idempotency_key=idempotency_key)
    except SupplierError as exc:
        if (
            provider == "lehai"
            and exc.code == "INSUFFICIENT_BALANCE"
            and idempotency_key
        ):
            try:
                snapshot = await client.fetch_snapshot(product_id)
            except SupplierError:
                snapshot = None
            if snapshot is not None and snapshot.effective_stock >= quantity:
                logger.warning(
                    "Le Hai purchase balance mismatch; retrying safely: "
                    "product=%s quantity=%s balance=%s unit_price=%s",
                    product_id,
                    quantity,
                    snapshot.owner_balance,
                    snapshot.unit_price,
                )
                await asyncio.sleep(0.5)
                try:
                    return await client.buy(
                        product_id,
                        quantity,
                        idempotency_key=idempotency_key,
                    )
                except SupplierError as retry_exc:
                    logger.warning(
                        "Le Hai safe retry failed: product=%s quantity=%s code=%s detail=%s",
                        product_id,
                        quantity,
                        retry_exc.code,
                        str(retry_exc),
                    )
                    raise retry_exc
        if exc.code not in RECOVERABLE_SUPPLIER_ERRORS:
            raise
        if provider != "sumistore":
            if not idempotency_key:
                raise
            try:
                return await client.buy(
                    product_id,
                    quantity,
                    idempotency_key=idempotency_key,
                )
            except SupplierError:
                raise exc
        recover_recent_purchase = getattr(client, "recover_recent_purchase", None)
        if recover_recent_purchase is None:
            raise
        try:
            recovered = await recover_recent_purchase(
                product_id,
                quantity,
                started_at=started_at,
                known_order_codes={code for code in known_order_codes if code},
            )
        except SupplierError:
            recovered = None
        if recovered is None:
            await queue_supplier_recovery(
                session,
                provider=provider,
                supplier_product_id=product_id,
                quantity=quantity,
                request_key=(
                    idempotency_key
                    or f"sumistore-recovery-{secrets.token_hex(16)}"
                ),
                started_at=started_at,
                error_code=exc.code,
            )
            raise exc
        logger.warning(
            "Recovered completed Sumi order after supplier response failure: order=%s",
            recovered.order_code,
        )
        return recovered


def supplier_client_for_source(
    fulfillment_source: str,
    sumistore_client: SumistoreClient | None,
    lehai_client: LeHaiPremiumClient | None,
) -> ExternalSupplierClient | None:
    if fulfillment_source == "sumistore":
        return sumistore_client
    if fulfillment_source == "lehai":
        return lehai_client
    return None


async def refresh_product_from_supplier(
    session: AsyncSession,
    product: Product,
    sumistore_client: SumistoreClient | None,
    lehai_client: LeHaiPremiumClient | None,
) -> int:
    if product.fulfillment_source == "sumistore":
        return await refresh_external_product(session, product, sumistore_client)
    if product.fulfillment_source == "lehai":
        return await refresh_lehai_product(session, product, lehai_client)
    return product.external_stock


async def ensure_user(
    session: AsyncSession,
    telegram_user: TelegramUser,
    referral_code: str | None = None,
) -> User:
    user = await session.get(User, telegram_user.id)
    if user is None:
        referrer_id = None
        normalized_referral = (referral_code or "").strip().upper()
        if normalized_referral:
            referrer_id = await session.scalar(
                select(User.telegram_id).where(User.referral_code == normalized_referral)
            )
        user = User(
            telegram_id=telegram_user.id,
            full_name=telegram_user.full_name,
            username=telegram_user.username,
            referred_by_id=referrer_id,
        )
        session.add(user)
        await session.flush()
    else:
        user.full_name = telegram_user.full_name
        user.username = telegram_user.username
    await ensure_referral_code(session, user)
    return user


async def available_stock(
    session: AsyncSession,
    product_id: int,
    supplier_client: SumistoreClient | None = None,
    *,
    lehai_client: LeHaiPremiumClient | None = None,
    refresh_external: bool = False,
) -> int:
    product = await session.get(Product, product_id)
    if product is None:
        return 0
    if product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES:
        if refresh_external:
            await refresh_product_from_supplier(
                session,
                product,
                supplier_client,
                lehai_client,
            )
            await session.commit()
        return max(0, product.external_stock)
    return int(
        await session.scalar(
            select(func.count(InventoryItem.id)).where(
                InventoryItem.product_id == product_id,
                InventoryItem.status == "available",
            )
        )
        or 0
    )


def normalize_discount_code(value: str) -> str:
    return value.strip().upper()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class ProductPricing:
    original_unit_price: int
    discount_per_unit: int
    final_unit_price: int
    coupon: DiscountCode | None = None
    coupon_discount_per_unit: int = 0
    quantity_discount_percent: int = 0
    quantity_discount_per_unit: int = 0


async def product_pricing(
    session: AsyncSession,
    product: Product,
    *,
    coupon_code: str | None = None,
    coupon_id: int | None = None,
    quantity: int = 1,
    lock_coupon: bool = False,
) -> ProductPricing | None:
    coupon: DiscountCode | None = None
    coupon_discount = 0
    if coupon_code or coupon_id is not None:
        statement = select(DiscountCode).where(DiscountCode.product_id == product.id)
        if coupon_id is not None:
            statement = statement.where(DiscountCode.id == coupon_id)
        else:
            statement = statement.where(
                DiscountCode.code == normalize_discount_code(coupon_code or "")
            )
        if lock_coupon:
            statement = statement.with_for_update()
        coupon = await session.scalar(statement)
        now = datetime.now(UTC)
        if (
            coupon is None
            or not coupon.active
            or (_as_utc(coupon.starts_at) is not None and _as_utc(coupon.starts_at) > now)
            or (_as_utc(coupon.expires_at) is not None and _as_utc(coupon.expires_at) <= now)
            or (coupon.max_uses > 0 and coupon.used_count >= coupon.max_uses)
        ):
            return None

        if coupon.discount_type == "percent":
            coupon_discount = product.price * coupon.discount_value // 100
        else:
            coupon_discount = coupon.discount_value
        coupon_discount = max(
            0,
            min(coupon_discount, max(0, product.price - 1)),
        )

    tier = await session.scalar(
        select(QuantityDiscount)
        .where(
            QuantityDiscount.product_id == product.id,
            QuantityDiscount.active.is_(True),
            QuantityDiscount.min_quantity <= max(1, quantity),
        )
        .order_by(
            QuantityDiscount.min_quantity.desc(),
            QuantityDiscount.discount_percent.desc(),
        )
        .limit(1)
    )
    quantity_percent = tier.discount_percent if tier is not None else 0
    raw_quantity_discount = product.price * quantity_percent // 100
    quantity_discount = max(
        0,
        min(
            raw_quantity_discount,
            max(0, product.price - coupon_discount - 1),
        ),
    )
    discount = coupon_discount + quantity_discount
    return ProductPricing(
        original_unit_price=product.price,
        discount_per_unit=discount,
        final_unit_price=product.price - discount,
        coupon=coupon,
        coupon_discount_per_unit=coupon_discount,
        quantity_discount_percent=quantity_percent,
        quantity_discount_per_unit=quantity_discount,
    )


async def active_quantity_discounts(
    session: AsyncSession,
    product_id: int,
) -> list[QuantityDiscount]:
    return list(
        await session.scalars(
            select(QuantityDiscount)
            .where(
                QuantityDiscount.product_id == product_id,
                QuantityDiscount.active.is_(True),
            )
            .order_by(QuantityDiscount.min_quantity, QuantityDiscount.discount_percent)
        )
    )


@dataclass
class PurchaseResult:
    ok: bool
    message: str
    orders: list[Order] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    total_amount: int = 0
    discount_amount: int = 0
    coupon_code: str | None = None
    quantity_discount_percent: int = 0

    @property
    def order(self) -> Order | None:
        return self.orders[0] if self.orders else None

    @property
    def secret(self) -> str | None:
        return self.secrets[0] if self.secrets else None


@dataclass(frozen=True)
class UserActivityStats:
    purchase_count: int
    purchased_items: int
    deposit_count: int
    total_spent: int
    total_deposited: int


async def user_activity_stats(session: AsyncSession, user_id: int) -> UserActivityStats:
    batch_purchases = int(
        await session.scalar(
            select(func.count(func.distinct(Order.batch_code))).where(
                Order.user_id == user_id,
                Order.batch_code.is_not(None),
            )
        )
        or 0
    )
    single_purchases = int(
        await session.scalar(
            select(func.count(Order.id)).where(
                Order.user_id == user_id,
                Order.batch_code.is_(None),
            )
        )
        or 0
    )
    purchased_items = int(
        await session.scalar(select(func.count(Order.id)).where(Order.user_id == user_id)) or 0
    )
    total_spent = int(
        await session.scalar(
            select(func.coalesce(func.sum(Order.amount), 0)).where(Order.user_id == user_id)
        )
        or 0
    )
    sms_purchase_count, sms_spent = (
        await session.execute(
            select(
                func.count(SmsRental.id),
                func.coalesce(func.sum(SmsRental.sale_amount), 0),
            ).where(
                SmsRental.user_id == user_id,
                SmsRental.status == "success",
            )
        )
    ).one()
    deposit_count = int(
        await session.scalar(
            select(func.count(PaymentTransaction.id)).where(
                PaymentTransaction.user_id == user_id,
                PaymentTransaction.credit_status == "credited",
            )
        )
        or 0
    )
    total_deposited = int(
        await session.scalar(
            select(func.coalesce(func.sum(PaymentTransaction.amount), 0)).where(
                PaymentTransaction.user_id == user_id,
                PaymentTransaction.credit_status == "credited",
            )
        )
        or 0
    )
    return UserActivityStats(
        purchase_count=batch_purchases + single_purchases + int(sms_purchase_count),
        purchased_items=purchased_items + int(sms_purchase_count),
        deposit_count=deposit_count,
        total_spent=total_spent + int(sms_spent),
        total_deposited=total_deposited,
    )


async def purchase_product(
    session_factory: async_sessionmaker[AsyncSession],
    telegram_id: int,
    product_id: int,
    cipher: SecretCipher,
    quantity: int = 1,
    supplier_client: SumistoreClient | None = None,
    *,
    lehai_client: LeHaiPremiumClient | None = None,
    coupon_code: str | None = None,
    coupon_id: int | None = None,
    sales_channel: str = "telegram",
    api_client_id: int | None = None,
    api_order_request_id: int | None = None,
    referral_commission_percent: int = 5,
    on_fulfillment_started: FulfillmentStartedCallback | None = None,
    supplier_idempotency_key: str | None = None,
) -> PurchaseResult:
    async with session_factory() as session:
        fulfillment_source = await session.scalar(
            select(Product.fulfillment_source).where(Product.id == product_id)
        )
    external_client = supplier_client_for_source(
        str(fulfillment_source or ""),
        supplier_client,
        lehai_client,
    )
    if external_client is not None:
        async with supplier_balance_guard(external_client):
            return await _purchase_product(
                session_factory,
                telegram_id,
                product_id,
                cipher,
                quantity,
                supplier_client,
                lehai_client,
                coupon_code=coupon_code,
                coupon_id=coupon_id,
                sales_channel=sales_channel,
                api_client_id=api_client_id,
                api_order_request_id=api_order_request_id,
                referral_commission_percent=referral_commission_percent,
                on_fulfillment_started=on_fulfillment_started,
                supplier_idempotency_key=supplier_idempotency_key,
            )
    return await _purchase_product(
        session_factory,
        telegram_id,
        product_id,
        cipher,
        quantity,
        supplier_client,
        lehai_client,
        coupon_code=coupon_code,
        coupon_id=coupon_id,
        sales_channel=sales_channel,
        api_client_id=api_client_id,
        api_order_request_id=api_order_request_id,
        referral_commission_percent=referral_commission_percent,
        on_fulfillment_started=on_fulfillment_started,
        supplier_idempotency_key=supplier_idempotency_key,
    )


async def _purchase_product(
    session_factory: async_sessionmaker[AsyncSession],
    telegram_id: int,
    product_id: int,
    cipher: SecretCipher,
    quantity: int,
    supplier_client: SumistoreClient | None,
    lehai_client: LeHaiPremiumClient | None,
    *,
    coupon_code: str | None,
    coupon_id: int | None,
    sales_channel: str,
    api_client_id: int | None,
    api_order_request_id: int | None,
    referral_commission_percent: int,
    on_fulfillment_started: FulfillmentStartedCallback | None,
    supplier_idempotency_key: str | None,
) -> PurchaseResult:
    async with session_factory() as session:
        async with session.begin():
            user = await session.scalar(
                select(User).where(User.telegram_id == telegram_id).with_for_update()
            )
            product = await session.get(Product, product_id)
            if user is None or product is None or not product.active:
                return PurchaseResult(False, "not_found")
            if user.is_blocked:
                return PurchaseResult(False, "blocked")
            if quantity < 1 or quantity > product.max_quantity:
                return PurchaseResult(False, "invalid_quantity")
            if quantity > 1 and not product.allow_quantity:
                return PurchaseResult(False, "invalid_quantity")
            external_client = supplier_client_for_source(
                product.fulfillment_source,
                supplier_client,
                lehai_client,
            )
            recovered_items: list[InventoryItem] = []
            if product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES:
                recovered_items = await reserve_available_inventory(
                    session,
                    product.id,
                    quantity,
                )
                if len(recovered_items) != quantity:
                    recovered_stock = len(recovered_items)
                    recovered_items = []
                    await refresh_product_from_supplier(
                        session,
                        product,
                        supplier_client,
                        lehai_client,
                    )
                    supplier_stock = max(0, product.external_stock - recovered_stock)
                    if (
                        external_client is None
                        or not product.supplier_product_id
                        or supplier_stock < quantity
                    ):
                        return PurchaseResult(False, "out_of_stock")
            pricing = await product_pricing(
                session,
                product,
                coupon_code=coupon_code,
                coupon_id=coupon_id,
                quantity=quantity,
                lock_coupon=bool(coupon_code or coupon_id is not None),
            )
            if pricing is None:
                return PurchaseResult(False, "invalid_coupon")
            total_amount = pricing.final_unit_price * quantity
            total_discount = pricing.discount_per_unit * quantity
            if user.balance < total_amount:
                return PurchaseResult(
                    False,
                    "insufficient",
                    total_amount=total_amount,
                    discount_amount=total_discount,
                    coupon_code=pricing.coupon.code if pricing.coupon else None,
                    quantity_discount_percent=pricing.quantity_discount_percent,
                )

            if product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES:
                sale_unit_price = pricing.final_unit_price
                if recovered_items:
                    now = datetime.now(UTC)
                    batch_code = f"B{secrets.token_hex(5).upper()}"
                    product.external_stock = max(0, product.external_stock - quantity)
                    orders = []
                    secret_values = []
                    for item in recovered_items:
                        item.status = "sold"
                        item.sold_at = now
                        order = Order(
                            user_id=user.telegram_id,
                            product_id=product.id,
                            inventory_item_id=item.id,
                            amount=sale_unit_price,
                            cost_amount=item.cost_amount,
                            discount_amount=pricing.discount_per_unit,
                            discount_code_id=pricing.coupon.id if pricing.coupon else None,
                            discount_code=pricing.coupon.code if pricing.coupon else None,
                            batch_code=batch_code,
                            supplier_order_code=item.supplier_order_code,
                            sales_channel=sales_channel,
                            api_client_id=api_client_id,
                            api_order_request_id=api_order_request_id,
                            status="completed",
                            delivered_at=now,
                            product=product,
                            inventory_item=item,
                        )
                        session.add(order)
                        orders.append(order)
                        secret_values.append(cipher.decrypt(item.encrypted_secret))
                    if pricing.coupon is not None:
                        pricing.coupon.used_count += 1
                    user.balance -= total_amount
                    await award_referral_commission(
                        session,
                        user,
                        shop_order_code=batch_code,
                        order_amount=total_amount,
                        sales_channel=sales_channel,
                        commission_percent=referral_commission_percent,
                    )
                    await session.flush()
                    return PurchaseResult(
                        True,
                        "completed",
                        orders=orders,
                        secrets=secret_values,
                        total_amount=total_amount,
                        discount_amount=total_discount,
                        coupon_code=pricing.coupon.code if pricing.coupon else None,
                        quantity_discount_percent=pricing.quantity_discount_percent,
                    )
                await notify_fulfillment_started(
                    on_fulfillment_started,
                    user.telegram_id,
                    user.language,
                )
                try:
                    supplier_purchase = await buy_supplier_product(
                        session,
                        external_client,
                        product.supplier_product_id,
                        quantity,
                        idempotency_key=(
                            supplier_idempotency_key
                            or f"shop-{secrets.token_hex(16)}"
                        ),
                    )
                except SupplierError as exc:
                    logger.warning(
                        "Supplier purchase failed: provider=%s product=%s quantity=%s "
                        "code=%s detail=%s",
                        product.fulfillment_source,
                        product.supplier_product_id,
                        quantity,
                        exc.code,
                        str(exc),
                    )
                    if exc.code == "INSUFFICIENT_STOCK":
                        product.external_stock = 0
                        return PurchaseResult(False, "out_of_stock")
                    # A balance/provider failure is not proof that the catalog
                    # item is sold out. Keep the last known stock for the next
                    # sync and report a temporary supplier failure.
                    return PurchaseResult(False, "supplier_unavailable")

                now = datetime.now(UTC)
                batch_code = f"B{secrets.token_hex(5).upper()}"
                if (
                    supplier_purchase.provider == "sumistore"
                    and supplier_purchase.unit_price > 0
                ):
                    await apply_supplier_price(
                        session,
                        product,
                        supplier_purchase.unit_price,
                    )
                cost_unit_price = max(
                    0,
                    supplier_purchase.unit_price
                    or product.supplier_price
                    or 0,
                )
                product.external_stock = max(0, product.external_stock - quantity)
                orders = []
                secret_values = []
                for item_index, secret_value in enumerate(supplier_purchase.accounts):
                    item = InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt(secret_value),
                        cost_amount=cost_unit_price,
                        supplier_order_code=supplier_purchase.order_code or None,
                        supplier_item_index=item_index,
                        status="sold",
                        sold_at=now,
                    )
                    session.add(item)
                    await session.flush()
                    order = Order(
                        user_id=user.telegram_id,
                        product_id=product.id,
                        inventory_item_id=item.id,
                        amount=sale_unit_price,
                        cost_amount=cost_unit_price,
                        discount_amount=pricing.discount_per_unit,
                        discount_code_id=pricing.coupon.id if pricing.coupon else None,
                        discount_code=pricing.coupon.code if pricing.coupon else None,
                        batch_code=batch_code,
                        supplier_order_code=supplier_purchase.order_code or None,
                        sales_channel=sales_channel,
                        api_client_id=api_client_id,
                        api_order_request_id=api_order_request_id,
                        status="completed",
                        delivered_at=now,
                        product=product,
                        inventory_item=item,
                    )
                    session.add(order)
                    orders.append(order)
                    secret_values.append(secret_value)
                record_supplier_purchase(
                    session,
                    amount=cost_unit_price * quantity,
                    supplier_order_code=supplier_purchase.order_code or None,
                    shop_order_code=batch_code,
                    product_id=product.id,
                    quantity=quantity,
                    provider=supplier_purchase.provider,
                )
                if pricing.coupon is not None:
                    pricing.coupon.used_count += 1
                user.balance -= total_amount
                await award_referral_commission(
                    session,
                    user,
                    shop_order_code=batch_code,
                    order_amount=total_amount,
                    sales_channel=sales_channel,
                    commission_percent=referral_commission_percent,
                )
                await session.flush()
                return PurchaseResult(
                    True,
                    "completed",
                    orders=orders,
                    secrets=secret_values,
                    total_amount=total_amount,
                    discount_amount=total_discount,
                    coupon_code=pricing.coupon.code if pricing.coupon else None,
                    quantity_discount_percent=pricing.quantity_discount_percent,
                )

            items = list(
                await session.scalars(
                    select(InventoryItem)
                    .where(
                        InventoryItem.product_id == product_id,
                        InventoryItem.status == "available",
                    )
                    .order_by(InventoryItem.id)
                    .with_for_update(skip_locked=True)
                    .limit(quantity)
                )
            )
            if len(items) != quantity:
                return PurchaseResult(False, "out_of_stock")

            now = datetime.now(UTC)
            batch_code = f"B{secrets.token_hex(5).upper()}"
            user.balance -= total_amount
            orders = []
            secret_values = []
            for item in items:
                item.status = "sold"
                item.sold_at = now
                order = Order(
                    user_id=user.telegram_id,
                    product_id=product.id,
                    inventory_item_id=item.id,
                    amount=pricing.final_unit_price,
                    cost_amount=item.cost_amount,
                    discount_amount=pricing.discount_per_unit,
                    discount_code_id=pricing.coupon.id if pricing.coupon else None,
                    discount_code=pricing.coupon.code if pricing.coupon else None,
                    batch_code=batch_code,
                    sales_channel=sales_channel,
                    api_client_id=api_client_id,
                    api_order_request_id=api_order_request_id,
                    status="completed",
                    delivered_at=now,
                    product=product,
                    inventory_item=item,
                )
                session.add(order)
                orders.append(order)
                secret_values.append(cipher.decrypt(item.encrypted_secret))
            if pricing.coupon is not None:
                pricing.coupon.used_count += 1
            await award_referral_commission(
                session,
                user,
                shop_order_code=batch_code,
                order_amount=total_amount,
                sales_channel=sales_channel,
                commission_percent=referral_commission_percent,
            )
            await session.flush()
            return PurchaseResult(
                True,
                "completed",
                orders=orders,
                secrets=secret_values,
                total_amount=total_amount,
                discount_amount=total_discount,
                coupon_code=pricing.coupon.code if pricing.coupon else None,
                quantity_discount_percent=pricing.quantity_discount_percent,
            )


class PendingDepositLimitReached(RuntimeError):
    pass


async def create_deposit(
    session: AsyncSession,
    user_id: int,
    amount: int,
    payment_prefix: str = "NAP",
    *,
    payment_kind: str = "wallet",
    product_id: int | None = None,
    quantity: int = 1,
    discount_amount: int = 0,
    discount_code_id: int | None = None,
    discount_code: str | None = None,
    expiry_seconds: int = 300,
    max_pending_deposits: int = 3,
) -> Deposit:
    now = datetime.now(UTC)
    user = await session.scalar(
        select(User).where(User.telegram_id == user_id).with_for_update()
    )
    if user is None:
        raise ValueError("User does not exist")

    # Reuse identical QR requests created in the last 30 seconds instead of growing the table.
    reusable_after = now + timedelta(seconds=max(1, expiry_seconds - 30))
    existing = await session.scalar(
        select(Deposit)
        .where(
            Deposit.user_id == user_id,
            Deposit.status == "pending",
            Deposit.expires_at.is_not(None),
            Deposit.expires_at >= reusable_after,
            Deposit.requested_amount == amount,
            Deposit.payment_kind == payment_kind,
            Deposit.product_id == product_id,
            Deposit.quantity == quantity,
            Deposit.discount_code_id == discount_code_id,
        )
        .order_by(Deposit.id.desc())
        .limit(1)
    )
    if existing is not None:
        await session.commit()
        return existing

    active_count = int(
        await session.scalar(
            select(func.count(Deposit.id)).where(
                Deposit.user_id == user_id,
                Deposit.status == "pending",
                Deposit.expires_at.is_not(None),
                Deposit.expires_at > now,
            )
        )
        or 0
    )
    if active_count >= max(1, max_pending_deposits):
        await session.rollback()
        raise PendingDepositLimitReached

    code = f"{payment_prefix.upper()}{user_id}{secrets.token_hex(2).upper()}"
    deposit = Deposit(
        user_id=user_id,
        code=code,
        requested_amount=amount,
        payment_kind=payment_kind,
        product_id=product_id,
        quantity=quantity,
        discount_amount=discount_amount,
        discount_code_id=discount_code_id,
        discount_code=discount_code,
        expires_at=now + timedelta(seconds=max(1, expiry_seconds)),
    )
    session.add(deposit)
    await session.commit()
    await session.refresh(deposit)
    return deposit


@dataclass
class PaymentResult:
    status: str
    user_id: int | None = None
    amount: int = 0
    product_id: int | None = None
    supplier_product_id: str | None = None
    product_name_vi: str | None = None
    product_name_en: str | None = None
    encrypted_secrets: tuple[str, ...] = ()
    order_ids: tuple[int, ...] = ()
    shop_order_code: str | None = None
    quantity: int = 1
    language: str = "vi"
    balance: int | None = None
    deposit_code: str | None = None
    username: str | None = None
    paid_at: datetime | None = None


async def process_sepay_payment(
    session_factory: async_sessionmaker[AsyncSession],
    payload: dict[str, object],
    payment_prefix: str = "NAP",
    cipher: SecretCipher | None = None,
    supplier_client: SumistoreClient | None = None,
    referral_commission_percent: int = 5,
    on_fulfillment_started: FulfillmentStartedCallback | None = None,
    lehai_client: LeHaiPremiumClient | None = None,
) -> PaymentResult:
    if supplier_client is not None or lehai_client is not None:
        payment_text = " ".join(
            str(payload.get(key) or "") for key in ("code", "content", "description")
        )
        deposit_code = find_deposit_code(payment_text, payment_prefix)
        if deposit_code is not None:
            async with session_factory() as session:
                supplier_source = await session.scalar(
                    select(Product.fulfillment_source)
                    .join(Deposit, Deposit.product_id == Product.id)
                    .where(
                        Deposit.code == deposit_code,
                        Deposit.payment_kind == "direct_purchase",
                        Product.fulfillment_source.in_(EXTERNAL_FULFILLMENT_SOURCES),
                    )
                )
            external_client = supplier_client_for_source(
                str(supplier_source or ""),
                supplier_client,
                lehai_client,
            )
            if external_client is not None:
                async with supplier_balance_guard(external_client):
                    return await _process_sepay_payment(
                        session_factory,
                        payload,
                        payment_prefix,
                        cipher,
                        supplier_client,
                        referral_commission_percent,
                        on_fulfillment_started,
                        lehai_client,
                    )
    return await _process_sepay_payment(
        session_factory,
        payload,
        payment_prefix,
        cipher,
        supplier_client,
        referral_commission_percent,
        on_fulfillment_started,
        lehai_client,
    )


async def _process_sepay_payment(
    session_factory: async_sessionmaker[AsyncSession],
    payload: dict[str, object],
    payment_prefix: str,
    cipher: SecretCipher | None,
    supplier_client: SumistoreClient | None,
    referral_commission_percent: int,
    on_fulfillment_started: FulfillmentStartedCallback | None,
    lehai_client: LeHaiPremiumClient | None,
) -> PaymentResult:
    transfer_type = str(payload.get("transferType") or payload.get("transfer_type") or "").lower()
    if transfer_type and transfer_type not in {"in", "credit", "incoming"}:
        return PaymentResult("ignored_outgoing")

    raw_amount = payload.get("transferAmount") or payload.get("amount") or 0
    try:
        amount = int(float(str(raw_amount)))
    except (TypeError, ValueError):
        return PaymentResult("invalid_amount")
    if amount <= 0:
        return PaymentResult("invalid_amount")

    provider_tx_id = str(
        payload.get("id") or payload.get("referenceCode") or payload.get("reference_code") or ""
    ).strip()
    if not provider_tx_id:
        return PaymentResult("missing_transaction_id")

    payment_text = " ".join(
        str(payload.get(key) or "") for key in ("code", "content", "description")
    )
    deposit_code = find_deposit_code(payment_text, payment_prefix)
    if deposit_code is None:
        return PaymentResult("deposit_not_found")

    async with session_factory() as session:
        async with session.begin():
            deposit = await session.scalar(
                select(Deposit).where(Deposit.code == deposit_code).with_for_update()
            )
            if deposit is None:
                return PaymentResult("deposit_not_found")
            existing = await session.scalar(
                select(PaymentTransaction).where(
                    PaymentTransaction.provider_tx_id == provider_tx_id
                )
            )
            if existing is not None:
                return PaymentResult("duplicate", existing.user_id, existing.amount)
            user = await session.scalar(
                select(User).where(User.telegram_id == deposit.user_id).with_for_update()
            )
            if user is None:
                return PaymentResult("user_not_found")

            now = datetime.now(UTC)
            expires_at = _as_utc(deposit.expires_at)
            if expires_at is None:
                created_at = _as_utc(deposit.created_at) or now
                expires_at = created_at + timedelta(minutes=5)

            rejected_status: str | None = None
            credit_status: str | None = None
            if now >= expires_at:
                if deposit.status == "pending":
                    deposit.status = "failed"
                    deposit.failed_at = now
                    deposit.failure_reason = "expired"
                rejected_status = "expired_payment"
                credit_status = "expired"
            elif deposit.status != "pending":
                rejected_status = (
                    "already_paid_payment"
                    if deposit.status == "paid"
                    else "failed_request_payment"
                )
                credit_status = (
                    "already_paid" if deposit.status == "paid" else "failed_request"
                )
            elif amount != deposit.requested_amount:
                rejected_status = "amount_mismatch"
                credit_status = "amount_mismatch"
            elif user.is_blocked:
                deposit.status = "failed"
                deposit.failed_at = now
                deposit.failure_reason = "blocked_user"
                rejected_status = "failed_request_payment"
                credit_status = "failed_request"
            elif deposit.payment_kind not in {"wallet", "direct_purchase"}:
                deposit.status = "failed"
                deposit.failed_at = now
                deposit.failure_reason = "invalid_payment_kind"
                rejected_status = "failed_request_payment"
                credit_status = "failed_request"
            elif deposit.payment_kind == "direct_purchase" and (
                deposit.product_id is None or deposit.quantity < 1
            ):
                deposit.status = "failed"
                deposit.failed_at = now
                deposit.failure_reason = "invalid_purchase_request"
                rejected_status = "failed_request_payment"
                credit_status = "failed_request"

            if rejected_status is not None and credit_status is not None:
                session.add(
                    PaymentTransaction(
                        deposit_id=deposit.id,
                        user_id=user.telegram_id,
                        provider_tx_id=provider_tx_id,
                        amount=amount,
                        credit_status=credit_status,
                    )
                )
                await session.flush()
                return PaymentResult(
                    rejected_status,
                    user.telegram_id,
                    amount,
                    quantity=deposit.quantity,
                    language=user.language,
                    balance=user.balance,
                    deposit_code=deposit.code,
                    username=user.username,
                    paid_at=now,
                )

            direct_purchase_ready = (
                deposit.payment_kind == "direct_purchase"
                and deposit.product_id is not None
            )
            if direct_purchase_ready:
                product = await session.get(Product, deposit.product_id)
                items: list[InventoryItem] = []
                supplier_purchase_made = False
                if product is not None and product.active:
                    if deposit.quantity == 1 or product.allow_quantity:
                        external_client = supplier_client_for_source(
                            product.fulfillment_source,
                            supplier_client,
                            lehai_client,
                        )
                        if product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES:
                            items = await reserve_available_inventory(
                                session,
                                product.id,
                                deposit.quantity,
                            )
                            if len(items) == deposit.quantity:
                                product.external_stock = max(
                                    0,
                                    product.external_stock - deposit.quantity,
                                )
                            else:
                                recovered_stock = len(items)
                                items = []
                                await notify_fulfillment_started(
                                    on_fulfillment_started,
                                    user.telegram_id,
                                    user.language,
                                )
                                await refresh_product_from_supplier(
                                    session,
                                    product,
                                    supplier_client,
                                    lehai_client,
                                )
                                supplier_stock = max(
                                    0,
                                    product.external_stock - recovered_stock,
                                )
                            if not items and (
                                cipher is not None
                                and external_client is not None
                                and product.supplier_product_id
                                and supplier_stock >= deposit.quantity
                            ):
                                try:
                                    supplier_purchase = await buy_supplier_product(
                                        session,
                                        external_client,
                                        product.supplier_product_id,
                                        deposit.quantity,
                                        idempotency_key=f"qr-{deposit.code}",
                                    )
                                except SupplierError:
                                    product.external_stock = recovered_stock
                                else:
                                    now = datetime.now(UTC)
                                    supplier_unit_cost = max(
                                        0,
                                        supplier_purchase.unit_price
                                        or product.supplier_price
                                        or 0,
                                    )
                                    if (
                                        supplier_purchase.provider == "sumistore"
                                        and supplier_purchase.unit_price > 0
                                    ):
                                        await apply_supplier_price(
                                            session,
                                            product,
                                            supplier_purchase.unit_price,
                                        )
                                    product.external_stock = max(
                                        0,
                                        product.external_stock - deposit.quantity,
                                    )
                                    items = [
                                        InventoryItem(
                                            product_id=product.id,
                                            encrypted_secret=cipher.encrypt(secret_value),
                                            cost_amount=supplier_unit_cost,
                                            supplier_order_code=(
                                                supplier_purchase.order_code or None
                                            ),
                                            supplier_item_index=item_index,
                                            status="sold",
                                            sold_at=now,
                                        )
                                        for item_index, secret_value in enumerate(
                                            supplier_purchase.accounts
                                        )
                                    ]
                                    supplier_purchase_made = True
                                    session.add_all(items)
                                    await session.flush()
                        else:
                            items = list(
                                await session.scalars(
                                    select(InventoryItem)
                                    .where(
                                        InventoryItem.product_id == product.id,
                                        InventoryItem.status == "available",
                                    )
                                    .order_by(InventoryItem.id)
                                    .with_for_update(skip_locked=True)
                                    .limit(deposit.quantity)
                                )
                            )
                if product is not None and len(items) == deposit.quantity:
                    batch_code = f"B{secrets.token_hex(5).upper()}"
                    orders = []
                    for item in items:
                        item.status = "sold"
                        item.sold_at = now
                        order = Order(
                            user_id=user.telegram_id,
                            product_id=product.id,
                            inventory_item_id=item.id,
                            amount=deposit.requested_amount // deposit.quantity,
                            cost_amount=item.cost_amount,
                            discount_amount=deposit.discount_amount // deposit.quantity,
                            discount_code_id=deposit.discount_code_id,
                            discount_code=deposit.discount_code,
                            batch_code=batch_code,
                            supplier_order_code=item.supplier_order_code,
                            sales_channel="telegram",
                            status="completed",
                            delivered_at=now,
                        )
                        session.add(order)
                        orders.append(order)
                    if (
                        product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES
                        and supplier_purchase_made
                    ):
                        record_supplier_purchase(
                            session,
                            amount=sum(item.cost_amount for item in items),
                            supplier_order_code=items[0].supplier_order_code,
                            shop_order_code=batch_code,
                            product_id=product.id,
                            quantity=deposit.quantity,
                            provider=supplier_purchase.provider,
                        )
                    if deposit.discount_code_id is not None:
                        coupon = await session.scalar(
                            select(DiscountCode)
                            .where(DiscountCode.id == deposit.discount_code_id)
                            .with_for_update()
                        )
                        if coupon is not None:
                            coupon.used_count += 1
                    await award_referral_commission(
                        session,
                        user,
                        shop_order_code=batch_code,
                        order_amount=amount,
                        sales_channel="telegram",
                        commission_percent=referral_commission_percent,
                    )
                    deposit.status = "paid"
                    deposit.paid_amount = amount
                    deposit.paid_at = now
                    session.add(
                        PaymentTransaction(
                            deposit_id=deposit.id,
                            user_id=user.telegram_id,
                            provider_tx_id=provider_tx_id,
                            amount=amount,
                            credit_status="credited",
                        )
                    )
                    await session.flush()
                    return PaymentResult(
                        "direct_purchase_completed",
                        user.telegram_id,
                        amount,
                        product_id=product.id,
                        supplier_product_id=product.supplier_product_id,
                        product_name_vi=product.name_vi,
                        product_name_en=product.name_en,
                        encrypted_secrets=tuple(item.encrypted_secret for item in items),
                        order_ids=tuple(order.id for order in orders),
                        shop_order_code=batch_code,
                        quantity=deposit.quantity,
                        language=user.language,
                        deposit_code=deposit.code,
                        username=user.username,
                        paid_at=now,
                    )

            user.balance += amount
            deposit.status = "paid"
            deposit.paid_amount = amount
            deposit.paid_at = now
            session.add(
                PaymentTransaction(
                    deposit_id=deposit.id,
                    user_id=user.telegram_id,
                    provider_tx_id=provider_tx_id,
                    amount=amount,
                    credit_status="credited",
                )
            )
            user_id = user.telegram_id
            is_direct_fallback = deposit.payment_kind == "direct_purchase"
            language = user.language

        return PaymentResult(
            "direct_purchase_fallback" if is_direct_fallback else "credited",
            user_id,
            amount,
            quantity=deposit.quantity,
            language=language,
            balance=user.balance,
            deposit_code=deposit.code,
            username=user.username,
            paid_at=deposit.paid_at,
        )


async def recent_orders(session: AsyncSession, user_id: int, limit: int = 10) -> list[Order]:
    result = list(await session.scalars(
        select(Order)
        .where(Order.user_id == user_id)
        .options(selectinload(Order.product), selectinload(Order.inventory_item))
        .order_by(Order.id.desc())
        .limit(max(1, limit) * 100)
    ))
    selected_keys: list[str] = []
    selected_key_set: set[str] = set()
    for order in result:
        key = order.shop_order_code
        if key in selected_key_set:
            continue
        if len(selected_keys) >= limit:
            break
        selected_keys.append(key)
        selected_key_set.add(key)
    return [order for order in result if order.shop_order_code in selected_key_set]


async def order_bundle(session: AsyncSession, user_id: int, order_id: int) -> list[Order]:
    order = await session.scalar(
        select(Order)
        .where(Order.id == order_id, Order.user_id == user_id)
        .options(selectinload(Order.product), selectinload(Order.inventory_item))
    )
    if order is None:
        return []
    if not order.batch_code:
        return [order]
    result = await session.scalars(
        select(Order)
        .where(Order.user_id == user_id, Order.batch_code == order.batch_code)
        .options(selectinload(Order.product), selectinload(Order.inventory_item))
        .order_by(Order.id)
    )
    return list(result)


async def active_categories(session: AsyncSession) -> list[Category]:
    result = await session.scalars(
        select(Category)
        .where(
            Category.active.is_(True),
            Category.products.any(
                Product.active.is_(True)
                & Product.fulfillment_source.in_(SELLABLE_FULFILLMENT_SOURCES)
                & (Product.product_type == "account")
            ),
        )
        .order_by(Category.position, Category.id)
    )
    return list(result)


async def active_products(session: AsyncSession, category_id: int | None = None) -> list[Product]:
    statement = (
        select(Product)
        .where(
            Product.active.is_(True),
            Product.fulfillment_source.in_(SELLABLE_FULFILLMENT_SOURCES),
            Product.product_type == "account",
        )
        .order_by(Product.id)
    )
    if category_id is not None:
        statement = statement.where(Product.category_id == category_id)
    result = await session.scalars(statement)
    return list(result)
