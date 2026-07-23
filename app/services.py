import asyncio
import hashlib
import logging
import secrets
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from aiogram.types import User as TelegramUser
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.flash_sales import (
    FlashSaleUnavailable,
    active_flash_sale,
    complete_deposit_flash_sale,
    consume_flash_sale,
    flash_sale_remaining,
    release_deposit_flash_sale,
    reserve_flash_sale,
    stop_unsafe_flash_sale,
)
from app.lehai_suppliers import LeHaiPremiumClient, refresh_lehai_product
from app.models import (
    BalanceAdjustment,
    Category,
    Deposit,
    DiscountCode,
    FlashSaleCampaign,
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
    WalletTransaction,
)
from app.price_alerts import apply_supplier_price, release_price_lock_if_inventory_empty
from app.stock_alerts import apply_supplier_stock
from app.partner_services import award_referral_commission, ensure_referral_code
from app.supplier_audit import record_supplier_purchase
from app.supplier_recovery import queue_supplier_recovery
from app.suppliers import (
    EXTERNAL_FULFILLMENT_SOURCES,
    SELLABLE_FULFILLMENT_SOURCES,
    ExternalSupplierClient,
    SupplierRoute,
    SupplierRouteFetch,
    SumistoreClient,
    SupplierError,
    SupplierPurchase,
    fetch_sumistore_supplier_routes,
    is_multi_supplier_product,
    plan_supplier_routes,
    refresh_external_product,
    supplier_balance_guard,
)
from app.utils import SecretCipher, find_deposit_code
from app.wallet_ledger import apply_wallet_change


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
    shop_product_id: int | None = None,
) -> SupplierPurchase:
    provider = getattr(client, "provider", "sumistore")
    request_key = idempotency_key or f"shop-{secrets.token_hex(16)}"
    if provider == "sumistore":
        recovery_key = (
            request_key
            if len(request_key) <= 96
            else hashlib.sha256(request_key.encode()).hexdigest()
        )
        pending_recovery = await session.scalar(
            select(SupplierRecoveryRequest.id).where(
                SupplierRecoveryRequest.request_key == recovery_key,
                SupplierRecoveryRequest.status == "pending",
            )
        )
        if pending_recovery is not None:
            raise SupplierError("SUPPLIER_RECOVERY_PENDING")
    product_db_id = shop_product_id
    if product_db_id is None:
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


async def preserve_supplier_purchase_for_resale(
    session: AsyncSession,
    product: Product,
    purchase: SupplierPurchase,
    cipher: SecretCipher,
    unit_cost: int,
) -> str:
    """Keep an already-paid supplier order sellable when a flash price becomes unsafe."""
    existing_indices: set[int] = set()
    if purchase.order_code:
        existing_indices = set(
            await session.scalars(
                select(InventoryItem.supplier_item_index).where(
                    InventoryItem.supplier_order_code == purchase.order_code,
                    InventoryItem.supplier_item_index.is_not(None),
                )
            )
        )
    inserted = 0
    for item_index, secret_value in enumerate(purchase.accounts):
        if item_index in existing_indices:
            continue
        session.add(
            InventoryItem(
                product_id=product.id,
                encrypted_secret=cipher.encrypt(secret_value),
                cost_amount=unit_cost,
                supplier_order_code=purchase.order_code or None,
                supplier_item_index=item_index,
                status="available",
            )
        )
        inserted += 1
    recovery_code = f"R{secrets.token_hex(5).upper()}"
    if inserted:
        record_supplier_purchase(
            session,
            amount=unit_cost * inserted,
            supplier_order_code=purchase.order_code or None,
            shop_order_code=recovery_code,
            product_id=product.id,
            quantity=inserted,
            provider=purchase.provider,
        )
    await session.flush()
    return recovery_code


def supplier_plan_request_key(
    base_key: str,
    route: SupplierRoute,
    position: int,
) -> str:
    raw_key = f"{base_key}:{position}:{route.provider}:{route.product_id}"
    if len(raw_key) <= 120:
        return raw_key
    return f"route-{hashlib.sha256(raw_key.encode()).hexdigest()}"


async def execute_supplier_route_plan(
    session: AsyncSession,
    product: Product,
    plan: tuple[tuple[SupplierRoute, int], ...],
    *,
    request_key: str,
    cipher: SecretCipher,
) -> tuple[tuple[SupplierPurchase, int], ...]:
    completed: list[tuple[SupplierPurchase, int]] = []
    try:
        for position, (route, route_quantity) in enumerate(plan, start=1):
            purchase = await buy_supplier_product(
                session,
                route.client,
                route.product_id,
                route_quantity,
                idempotency_key=supplier_plan_request_key(
                    request_key,
                    route,
                    position,
                ),
                shop_product_id=product.id,
            )
            unit_cost = max(
                0,
                int(purchase.unit_price or route.snapshot.unit_price or 0),
            )
            completed.append((purchase, unit_cost))
    except SupplierError:
        # Never deliver half an order. Already-paid supplier accounts become
        # local stock so the customer's wallet/QR flow can fail atomically.
        for purchase, unit_cost in completed:
            await preserve_supplier_purchase_for_resale(
                session,
                product,
                purchase,
                cipher,
                unit_cost,
            )
        raise
    return tuple(completed)


async def preserve_supplier_purchase_parts(
    session: AsyncSession,
    product: Product,
    purchases: tuple[tuple[SupplierPurchase, int], ...],
    cipher: SecretCipher,
) -> None:
    for purchase, unit_cost in purchases:
        await preserve_supplier_purchase_for_resale(
            session,
            product,
            purchase,
            cipher,
            unit_cost,
        )


async def _execute_supplier_purchase(
    session: AsyncSession,
    client: ExternalSupplierClient,
    product_id: str,
    quantity: int,
    *,
    idempotency_key: str | None = None,
) -> SupplierPurchase:
    provider = getattr(client, "provider", "sumistore")
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
        # Building the known-order set can grow with the lifetime of the shop.
        # It is only needed after an ambiguous Sumi response, not on every
        # successful Sumi purchase or any idempotent Le Hai request.
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
        return await refresh_external_product(
            session,
            product,
            sumistore_client,
            lehai_client=lehai_client,
        )
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
    refresh_max_age_seconds: int = 0,
) -> int:
    product = await session.get(Product, product_id)
    if product is None:
        return 0
    if product.force_out_of_stock:
        return 0
    if product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES:
        if refresh_external:
            max_age = max(0, refresh_max_age_seconds)
            synced_at = _as_utc(product.supplier_synced_at)
            if (
                max_age > 0
                and synced_at is not None
                and synced_at > datetime.now(UTC) - timedelta(seconds=max_age)
            ):
                return max(0, product.external_stock)
            if max_age > 0:
                locked_product = await session.scalar(
                    select(Product)
                    .where(Product.id == product_id)
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
                if locked_product is None:
                    return max(0, product.external_stock)
                product = locked_product
                synced_at = _as_utc(product.supplier_synced_at)
                if (
                    synced_at is not None
                    and synced_at > datetime.now(UTC) - timedelta(seconds=max_age)
                ):
                    return max(0, product.external_stock)
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


def purchase_quantity_limit(product: Product, stock: int) -> int:
    """Return the largest quantity that can be selected from current stock."""
    return max(0, min(max(1, int(product.max_quantity)), max(0, int(stock))))


def normalize_discount_code(value: str) -> str:
    return value.strip().upper()


class CouponValidationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def resolve_discount_code(
    session: AsyncSession,
    product: Product,
    *,
    coupon_code: str | None = None,
    coupon_id: int | None = None,
    user_id: int | None = None,
    lock_coupon: bool = False,
) -> DiscountCode:
    normalized_code = normalize_discount_code(coupon_code or "")
    if coupon_id is None and not normalized_code:
        raise CouponValidationError("coupon_empty")

    statement = select(DiscountCode)
    if coupon_id is not None:
        statement = statement.where(DiscountCode.id == coupon_id)
    else:
        statement = statement.where(DiscountCode.code == normalized_code)
    if lock_coupon:
        statement = statement.with_for_update()
    coupon = await session.scalar(statement)
    if coupon is None:
        raise CouponValidationError("coupon_not_found")
    if coupon.product_id != product.id:
        raise CouponValidationError("coupon_wrong_product")

    now = datetime.now(UTC)
    starts_at = _as_utc(coupon.starts_at)
    expires_at = _as_utc(coupon.expires_at)
    if not coupon.active:
        raise CouponValidationError("coupon_inactive")
    if starts_at is not None and starts_at > now:
        raise CouponValidationError("coupon_not_started")
    if expires_at is not None and expires_at <= now:
        raise CouponValidationError("coupon_expired")
    if user_id is not None:
        previous_order = await session.scalar(
            select(Order.id)
            .where(
                Order.user_id == user_id,
                Order.discount_code_id == coupon.id,
            )
            .limit(1)
        )
        if previous_order is not None:
            raise CouponValidationError("coupon_already_used")
    if coupon.max_uses > 0 and coupon.used_count >= coupon.max_uses:
        raise CouponValidationError("coupon_exhausted")
    return coupon


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
    flash_sale: FlashSaleCampaign | None = None


@dataclass(frozen=True)
class SupplierAllocationPricing:
    route: SupplierRoute
    quantity: int
    original_unit_price: int
    final_unit_price: int
    discount_per_unit: int
    coupon_discount_per_unit: int
    quantity_discount_per_unit: int


@dataclass(frozen=True)
class MultiSupplierQuote:
    allocations: tuple[SupplierAllocationPricing, ...]
    total_amount: int
    discount_amount: int
    coupon_discount_amount: int
    quantity_discount_amount: int

    @property
    def available(self) -> bool:
        return bool(self.allocations)


def price_supplier_plan(
    product: Product,
    plan: tuple[tuple[SupplierRoute, int], ...],
    pricing: ProductPricing,
) -> MultiSupplierQuote:
    allocations: list[SupplierAllocationPricing] = []
    for route, route_quantity in plan:
        original_unit_price = (
            int(product.price)
            if product.price_lock_enabled
            else int(route.snapshot.unit_price) + max(0, int(product.supplier_markup))
        )
        if pricing.flash_sale is not None:
            final_unit_price = int(pricing.flash_sale.sale_price)
            coupon_discount = 0
            quantity_discount = 0
        else:
            coupon_discount = 0
            if pricing.coupon is not None:
                if pricing.coupon.discount_type == "percent":
                    coupon_discount = (
                        original_unit_price * pricing.coupon.discount_value // 100
                    )
                else:
                    coupon_discount = pricing.coupon.discount_value
                coupon_discount = max(
                    0,
                    min(coupon_discount, max(0, original_unit_price - 1)),
                )
            raw_quantity_discount = (
                original_unit_price * pricing.quantity_discount_percent // 100
            )
            quantity_discount = max(
                0,
                min(
                    raw_quantity_discount,
                    max(0, original_unit_price - coupon_discount - 1),
                ),
            )
            final_unit_price = (
                original_unit_price - coupon_discount - quantity_discount
            )
        discount_per_unit = max(0, original_unit_price - final_unit_price)
        allocations.append(
            SupplierAllocationPricing(
                route=route,
                quantity=route_quantity,
                original_unit_price=original_unit_price,
                final_unit_price=final_unit_price,
                discount_per_unit=discount_per_unit,
                coupon_discount_per_unit=coupon_discount,
                quantity_discount_per_unit=quantity_discount,
            )
        )
    return MultiSupplierQuote(
        allocations=tuple(allocations),
        total_amount=sum(
            allocation.final_unit_price * allocation.quantity
            for allocation in allocations
        ),
        discount_amount=sum(
            allocation.discount_per_unit * allocation.quantity
            for allocation in allocations
        ),
        coupon_discount_amount=sum(
            allocation.coupon_discount_per_unit * allocation.quantity
            for allocation in allocations
        ),
        quantity_discount_amount=sum(
            allocation.quantity_discount_per_unit * allocation.quantity
            for allocation in allocations
        ),
    )


async def multi_supplier_quote(
    product: Product,
    quantity: int,
    pricing: ProductPricing,
    sumistore_client: SumistoreClient | None,
    lehai_client: LeHaiPremiumClient | None,
) -> MultiSupplierQuote | None:
    if not is_multi_supplier_product(
        product.fulfillment_source,
        product.supplier_product_id,
    ) or not product.supplier_product_id:
        return None
    fetched = await fetch_sumistore_supplier_routes(
        product.supplier_product_id,
        sumistore_client,
        lehai_client,
    )
    plan = plan_supplier_routes(fetched.routes, quantity)
    if not plan:
        return MultiSupplierQuote((), 0, 0, 0, 0)
    return price_supplier_plan(product, plan, pricing)


async def product_pricing(
    session: AsyncSession,
    product: Product,
    *,
    coupon_code: str | None = None,
    coupon_id: int | None = None,
    quantity: int = 1,
    user_id: int | None = None,
    lock_coupon: bool = False,
    lock_flash_sale: bool = False,
    expected_flash_sale_id: int | None = None,
    raise_coupon_error: bool = False,
) -> ProductPricing | None:
    flash_sale = await active_flash_sale(
        session,
        product.id,
        quantity=1,
        for_update=lock_flash_sale,
        campaign_id=expected_flash_sale_id,
    )
    if expected_flash_sale_id is not None and flash_sale is None:
        return None
    if flash_sale is not None:
        return ProductPricing(
            original_unit_price=product.price,
            discount_per_unit=max(0, product.price - flash_sale.sale_price),
            final_unit_price=flash_sale.sale_price,
            flash_sale=flash_sale,
        )

    coupon: DiscountCode | None = None
    coupon_discount = 0
    if coupon_code is not None or coupon_id is not None:
        try:
            coupon = await resolve_discount_code(
                session,
                product,
                coupon_code=coupon_code,
                coupon_id=coupon_id,
                user_id=user_id,
                lock_coupon=lock_coupon,
            )
        except CouponValidationError:
            if raise_coupon_error:
                raise
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
    flash_sale_id: int | None = None

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
    expected_flash_sale_id: int | None = None,
    max_unit_price: int | None = None,
) -> PurchaseResult:
    async with session_factory() as session:
        source_row = (
            await session.execute(
                select(
                    Product.fulfillment_source,
                    Product.supplier_product_id,
                ).where(Product.id == product_id)
            )
        ).one_or_none()
    fulfillment_source = str(source_row[0]) if source_row is not None else ""
    supplier_product_id = source_row[1] if source_row is not None else None
    external_clients: list[ExternalSupplierClient] = []
    primary_client = supplier_client_for_source(
        fulfillment_source,
        supplier_client,
        lehai_client,
    )
    if primary_client is not None:
        external_clients.append(primary_client)
    if (
        is_multi_supplier_product(fulfillment_source, supplier_product_id)
        and lehai_client is not None
        and lehai_client not in external_clients
    ):
        external_clients.append(lehai_client)
    if external_clients:
        unique_clients = {
            id(client): client for client in external_clients
        }.values()
        ordered_clients = sorted(
            unique_clients,
            key=lambda client: (
                0 if getattr(client, "provider", "") == "sumistore" else 1,
                getattr(client, "provider", ""),
            ),
        )
        async with AsyncExitStack() as stack:
            for client in ordered_clients:
                await stack.enter_async_context(supplier_balance_guard(client))
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
                expected_flash_sale_id=expected_flash_sale_id,
                max_unit_price=max_unit_price,
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
        expected_flash_sale_id=expected_flash_sale_id,
        max_unit_price=max_unit_price,
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
    expected_flash_sale_id: int | None,
    max_unit_price: int | None,
) -> PurchaseResult:
    async with session_factory() as session:
        async with session.begin():
            user = await session.scalar(
                select(User).where(User.telegram_id == telegram_id).with_for_update()
            )
            product = await session.scalar(
                select(Product).where(Product.id == product_id).with_for_update()
            )
            if user is None or product is None or not product.active:
                return PurchaseResult(False, "not_found")
            if product.force_out_of_stock:
                return PurchaseResult(False, "out_of_stock")
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
            recovered_stock = 0
            multi_route_fetch: SupplierRouteFetch | None = None
            multi_plan: tuple[tuple[SupplierRoute, int], ...] = ()
            if product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES:
                recovered_items = await reserve_available_inventory(
                    session,
                    product.id,
                    quantity,
                )
                if len(recovered_items) != quantity:
                    recovered_stock = len(recovered_items)
                    recovered_items = []
                    if (
                        is_multi_supplier_product(
                            product.fulfillment_source,
                            product.supplier_product_id,
                        )
                        and product.supplier_product_id
                    ):
                        multi_route_fetch = await fetch_sumistore_supplier_routes(
                            product.supplier_product_id,
                            supplier_client,
                            lehai_client,
                        )
                        await refresh_external_product(
                            session,
                            product,
                            supplier_client,
                            lehai_client=lehai_client,
                            route_fetch=multi_route_fetch,
                        )
                        multi_plan = plan_supplier_routes(
                            multi_route_fetch.routes,
                            quantity,
                        )
                    else:
                        await refresh_product_from_supplier(
                            session,
                            product,
                            supplier_client,
                            lehai_client,
                        )
                    supplier_stock = max(0, product.external_stock - recovered_stock)
                    if (
                        not product.supplier_product_id
                        or supplier_stock < quantity
                        or (
                            multi_route_fetch is not None
                            and not multi_plan
                        )
                        or (
                            multi_route_fetch is None
                            and external_client is None
                        )
                    ):
                        return PurchaseResult(
                            False,
                            (
                                "supplier_unavailable"
                                if multi_route_fetch is not None
                                and not multi_route_fetch.routes
                                and multi_route_fetch.failures
                                else "out_of_stock"
                            ),
                        )
            try:
                pricing = await product_pricing(
                    session,
                    product,
                    coupon_code=coupon_code,
                    coupon_id=coupon_id,
                    quantity=quantity,
                    user_id=user.telegram_id,
                    lock_coupon=bool(coupon_code or coupon_id is not None),
                    lock_flash_sale=True,
                    expected_flash_sale_id=expected_flash_sale_id,
                    raise_coupon_error=True,
                )
            except CouponValidationError as exc:
                return PurchaseResult(False, exc.code)
            if pricing is None:
                return PurchaseResult(
                    False,
                    (
                        "flash_sale_unavailable"
                        if expected_flash_sale_id is not None
                        else "invalid_coupon"
                    ),
                    flash_sale_id=expected_flash_sale_id,
                )
            if (
                pricing.flash_sale is not None
                and flash_sale_remaining(pricing.flash_sale) < quantity
            ):
                return PurchaseResult(False, "out_of_stock")
            multi_quote = (
                price_supplier_plan(product, multi_plan, pricing)
                if multi_plan
                else None
            )
            if (
                multi_quote is not None
                and pricing.flash_sale is not None
                and any(
                    allocation.route.snapshot.unit_price
                    > allocation.final_unit_price
                    for allocation in multi_quote.allocations
                )
            ):
                return PurchaseResult(
                    False,
                    "flash_sale_unavailable",
                    flash_sale_id=pricing.flash_sale.id,
                )
            highest_unit_price = (
                max(
                    allocation.final_unit_price
                    for allocation in multi_quote.allocations
                )
                if multi_quote is not None
                else pricing.final_unit_price
            )
            total_amount = (
                multi_quote.total_amount
                if multi_quote is not None
                else pricing.final_unit_price * quantity
            )
            total_discount = (
                multi_quote.discount_amount
                if multi_quote is not None
                else pricing.discount_per_unit * quantity
            )
            if max_unit_price is not None and highest_unit_price > max_unit_price:
                return PurchaseResult(
                    False,
                    "price_changed",
                    total_amount=total_amount,
                    discount_amount=total_discount,
                    coupon_code=pricing.coupon.code if pricing.coupon else None,
                    quantity_discount_percent=pricing.quantity_discount_percent,
                    flash_sale_id=(pricing.flash_sale.id if pricing.flash_sale else None),
                )
            if user.balance < total_amount:
                return PurchaseResult(
                    False,
                    "insufficient",
                    total_amount=total_amount,
                    discount_amount=total_discount,
                    coupon_code=pricing.coupon.code if pricing.coupon else None,
                    quantity_discount_percent=pricing.quantity_discount_percent,
                    flash_sale_id=(pricing.flash_sale.id if pricing.flash_sale else None),
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
                            flash_sale_id=(
                                pricing.flash_sale.id if pricing.flash_sale else None
                            ),
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
                    apply_wallet_change(
                        session,
                        user,
                        -total_amount,
                        kind="product_purchase",
                        event_key=f"purchase:{batch_code}",
                        reference_type="order",
                        reference_id=batch_code,
                        description=(
                            f"Mua {quantity} tài khoản {product.name_vi} "
                            f"qua {sales_channel}"
                        ),
                    )
                    await award_referral_commission(
                        session,
                        user,
                        shop_order_code=batch_code,
                        order_amount=total_amount,
                        sales_channel=sales_channel,
                        commission_percent=referral_commission_percent,
                    )
                    consume_flash_sale(pricing.flash_sale, quantity)
                    await release_price_lock_if_inventory_empty(session, product)
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
                request_key = (
                    supplier_idempotency_key
                    or f"shop-{secrets.token_hex(16)}"
                )
                try:
                    if multi_plan:
                        supplier_purchases = await execute_supplier_route_plan(
                            session,
                            product,
                            multi_plan,
                            request_key=request_key,
                            cipher=cipher,
                        )
                    else:
                        supplier_purchase = await buy_supplier_product(
                            session,
                            external_client,
                            product.supplier_product_id,
                            quantity,
                            idempotency_key=request_key,
                            shop_product_id=product.id,
                        )
                        supplier_purchases = (
                            (
                                supplier_purchase,
                                max(
                                    0,
                                    supplier_purchase.unit_price
                                    or product.supplier_price
                                    or 0,
                                ),
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
                    if multi_plan:
                        if exc.code in {
                            "INSUFFICIENT_STOCK",
                            "INSUFFICIENT_BALANCE",
                            "SUPPLIER_PURCHASE_BACKOFF",
                        }:
                            return PurchaseResult(False, "out_of_stock")
                        return PurchaseResult(False, "supplier_unavailable")
                    purchase_is_blocked = getattr(
                        external_client,
                        "purchase_is_blocked",
                        None,
                    )
                    if callable(purchase_is_blocked) and purchase_is_blocked(
                        product.supplier_product_id
                    ):
                        product.external_stock = recovered_stock
                        await apply_supplier_stock(
                            session,
                            product,
                            0,
                            notify_on_increase=False,
                            local_inventory_stock=recovered_stock,
                        )
                    if exc.code in {
                        "INSUFFICIENT_STOCK",
                        "SUPPLIER_PURCHASE_BACKOFF",
                    }:
                        product.external_stock = recovered_stock
                        return PurchaseResult(False, "out_of_stock")
                    # A balance/provider failure is not proof that the catalog
                    # item is sold out. Keep the last known stock for the next
                    # sync and report a temporary supplier failure.
                    return PurchaseResult(False, "supplier_unavailable")

                now = datetime.now(UTC)
                batch_code = f"B{secrets.token_hex(5).upper()}"
                if not multi_plan and supplier_purchase.unit_price > 0 and (
                    supplier_purchase.provider == "sumistore"
                    or (
                        pricing.flash_sale is not None
                        and supplier_purchase.unit_price
                        > pricing.flash_sale.sale_price
                    )
                ):
                    await apply_supplier_price(
                        session,
                        product,
                        supplier_purchase.unit_price,
                    )
                if multi_plan and any(
                    unit_cost > allocation.route.snapshot.unit_price
                    for (_, unit_cost), allocation in zip(
                        supplier_purchases,
                        multi_quote.allocations,
                        strict=True,
                    )
                ):
                    await preserve_supplier_purchase_parts(
                        session,
                        product,
                        supplier_purchases,
                        cipher,
                    )
                    logger.warning(
                        "Multi-supplier cost changed during purchase; accounts moved to stock: "
                        "product=%s",
                        product.id,
                    )
                    return PurchaseResult(
                        False,
                        (
                            "flash_sale_unavailable"
                            if pricing.flash_sale is not None
                            else "price_changed"
                        ),
                        flash_sale_id=(
                            pricing.flash_sale.id if pricing.flash_sale else None
                        ),
                    )
                if not multi_plan:
                    cost_unit_price = supplier_purchases[0][1]
                    if (
                        pricing.flash_sale is not None
                        and cost_unit_price > pricing.flash_sale.sale_price
                    ):
                        recovery_code = await preserve_supplier_purchase_for_resale(
                            session,
                            product,
                            supplier_purchase,
                            cipher,
                            cost_unit_price,
                        )
                        logger.warning(
                            "Flash sale stopped after supplier cost increased during purchase: "
                            "campaign=%s product=%s sale_price=%s cost=%s recovery=%s",
                            pricing.flash_sale.id,
                            product.id,
                            pricing.flash_sale.sale_price,
                            cost_unit_price,
                            recovery_code,
                        )
                        return PurchaseResult(
                            False,
                            "flash_sale_unavailable",
                            flash_sale_id=pricing.flash_sale.id,
                        )
                product.external_stock = max(0, product.external_stock - quantity)
                orders = []
                secret_values = []
                if multi_plan:
                    allocation_rows = tuple(
                        (
                            purchase,
                            unit_cost,
                            allocation.final_unit_price,
                            allocation.discount_per_unit,
                        )
                        for (purchase, unit_cost), allocation in zip(
                            supplier_purchases,
                            multi_quote.allocations,
                            strict=True,
                        )
                    )
                else:
                    allocation_rows = (
                        (
                            supplier_purchase,
                            supplier_purchases[0][1],
                            sale_unit_price,
                            pricing.discount_per_unit,
                        ),
                    )
                for purchase, unit_cost, unit_sale_price, unit_discount in allocation_rows:
                    for item_index, secret_value in enumerate(purchase.accounts):
                        item = InventoryItem(
                            product_id=product.id,
                            encrypted_secret=cipher.encrypt(secret_value),
                            cost_amount=unit_cost,
                            supplier_order_code=purchase.order_code or None,
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
                            amount=unit_sale_price,
                            cost_amount=unit_cost,
                            discount_amount=unit_discount,
                            discount_code_id=(
                                pricing.coupon.id if pricing.coupon else None
                            ),
                            discount_code=(
                                pricing.coupon.code if pricing.coupon else None
                            ),
                            flash_sale_id=(
                                pricing.flash_sale.id if pricing.flash_sale else None
                            ),
                            batch_code=batch_code,
                            supplier_order_code=purchase.order_code or None,
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
                        amount=unit_cost * len(purchase.accounts),
                        supplier_order_code=purchase.order_code or None,
                        shop_order_code=batch_code,
                        product_id=product.id,
                        quantity=len(purchase.accounts),
                        provider=purchase.provider,
                    )
                if pricing.coupon is not None:
                    pricing.coupon.used_count += 1
                apply_wallet_change(
                    session,
                    user,
                    -total_amount,
                    kind="product_purchase",
                    event_key=f"purchase:{batch_code}",
                    reference_type="order",
                    reference_id=batch_code,
                    description=(
                        f"Mua {quantity} tài khoản {product.name_vi} qua {sales_channel}"
                    ),
                )
                await award_referral_commission(
                    session,
                    user,
                    shop_order_code=batch_code,
                    order_amount=total_amount,
                    sales_channel=sales_channel,
                    commission_percent=referral_commission_percent,
                )
                consume_flash_sale(pricing.flash_sale, quantity)
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
            apply_wallet_change(
                session,
                user,
                -total_amount,
                kind="product_purchase",
                event_key=f"purchase:{batch_code}",
                reference_type="order",
                reference_id=batch_code,
                description=(
                    f"Mua {quantity} tài khoản {product.name_vi} qua {sales_channel}"
                ),
            )
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
                    flash_sale_id=pricing.flash_sale.id if pricing.flash_sale else None,
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
            consume_flash_sale(pricing.flash_sale, quantity)
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
    flash_sale_id: int | None = None,
    flash_sale_quantity: int = 0,
    expiry_seconds: int = 300,
    max_pending_deposits: int = 3,
) -> Deposit:
    now = datetime.now(UTC)
    user = await session.scalar(
        select(User).where(User.telegram_id == user_id).with_for_update()
    )
    if user is None:
        raise ValueError("User does not exist")

    inventory_price_locked = False
    if payment_kind == "direct_purchase" and product_id is not None:
        deposit_product = await session.scalar(
            select(Product).where(Product.id == product_id).with_for_update()
        )
        inventory_price_locked = bool(
            deposit_product is not None and deposit_product.price_lock_enabled
        )

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
            Deposit.flash_sale_id == flash_sale_id,
            Deposit.inventory_price_locked == inventory_price_locked,
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

    flash_sale: FlashSaleCampaign | None = None
    if flash_sale_id is not None:
        flash_sale = await session.scalar(
            select(FlashSaleCampaign)
            .where(FlashSaleCampaign.id == flash_sale_id)
            .with_for_update()
        )
        reserved_quantity = max(1, flash_sale_quantity or quantity)
        if (
            flash_sale is None
            or flash_sale.status != "active"
            or flash_sale.product_id != product_id
            or amount != flash_sale.sale_price * quantity
        ):
            raise FlashSaleUnavailable("Flash sale is no longer available")
        reserve_flash_sale(flash_sale, reserved_quantity)

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
        flash_sale_id=flash_sale.id if flash_sale is not None else None,
        flash_sale_quantity=(
            max(1, flash_sale_quantity or quantity) if flash_sale is not None else 0
        ),
        inventory_price_locked=inventory_price_locked,
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


@dataclass(frozen=True)
class ManualDepositApprovalResult:
    status: str
    user_id: int | None = None
    amount: int = 0
    balance: int = 0
    deposit_code: str = ""
    username: str | None = None
    language: str = "vi"


@dataclass(frozen=True)
class ManualDepositCancellationResult:
    status: str
    user_id: int | None = None
    amount: int = 0
    balance: int = 0
    deposit_code: str = ""
    username: str | None = None
    language: str = "vi"


async def approve_wallet_deposit(
    session_factory: async_sessionmaker[AsyncSession],
    deposit_id: int,
    *,
    admin_username: str,
) -> ManualDepositApprovalResult:
    async with session_factory() as session:
        async with session.begin():
            deposit = await session.scalar(
                select(Deposit).where(Deposit.id == deposit_id).with_for_update()
            )
            if deposit is None:
                return ManualDepositApprovalResult("not_found")
            if deposit.payment_kind != "wallet":
                return ManualDepositApprovalResult("invalid_kind", deposit_code=deposit.code)
            if deposit.status == "paid":
                return ManualDepositApprovalResult("already_paid", deposit_code=deposit.code)
            if deposit.status not in {"pending", "failed"}:
                return ManualDepositApprovalResult("invalid_status", deposit_code=deposit.code)

            existing_credit = await session.scalar(
                select(PaymentTransaction.id).where(
                    PaymentTransaction.deposit_id == deposit.id,
                    PaymentTransaction.credit_status == "credited",
                )
            )
            existing_ledger = await session.scalar(
                select(WalletTransaction.id).where(
                    WalletTransaction.event_key == f"deposit:{deposit.id}"
                )
            )
            if existing_credit is not None or existing_ledger is not None:
                return ManualDepositApprovalResult(
                    "already_credited",
                    deposit_code=deposit.code,
                )

            user = await session.scalar(
                select(User).where(User.telegram_id == deposit.user_id).with_for_update()
            )
            if user is None:
                return ManualDepositApprovalResult("user_not_found", deposit_code=deposit.code)

            approved_at = datetime.now(UTC)
            amount = int(deposit.requested_amount)
            session.add(
                PaymentTransaction(
                    deposit_id=deposit.id,
                    user_id=user.telegram_id,
                    provider_tx_id=f"ADMIN-DEPOSIT-{deposit.id}",
                    amount=amount,
                    credit_status="credited",
                )
            )
            session.add(
                BalanceAdjustment(
                    user_id=user.telegram_id,
                    admin_username=admin_username,
                    amount=amount,
                    reason=f"Duyệt nạp thủ công mã {deposit.code}",
                )
            )
            apply_wallet_change(
                session,
                user,
                amount,
                kind="deposit",
                event_key=f"deposit:{deposit.id}",
                reference_type="deposit",
                reference_id=deposit.code,
                description=f"Admin {admin_username} duyệt nạp thủ công mã {deposit.code}",
            )
            deposit.status = "paid"
            deposit.paid_amount = amount
            deposit.paid_at = approved_at
            deposit.failed_at = None
            deposit.failure_reason = None
            return ManualDepositApprovalResult(
                "approved",
                user_id=user.telegram_id,
                amount=amount,
                balance=user.balance,
                deposit_code=deposit.code,
                username=user.username,
                language=user.language,
            )


async def cancel_wallet_deposit(
    session_factory: async_sessionmaker[AsyncSession],
    deposit_id: int,
) -> ManualDepositCancellationResult:
    async with session_factory() as session:
        async with session.begin():
            deposit = await session.scalar(
                select(Deposit).where(Deposit.id == deposit_id).with_for_update()
            )
            if deposit is None:
                return ManualDepositCancellationResult("not_found")
            if deposit.payment_kind != "wallet":
                return ManualDepositCancellationResult(
                    "invalid_kind",
                    deposit_code=deposit.code,
                )
            if deposit.status == "paid":
                return ManualDepositCancellationResult(
                    "already_paid",
                    deposit_code=deposit.code,
                )
            if deposit.status == "failed" and deposit.failure_reason == "admin_cancelled":
                return ManualDepositCancellationResult(
                    "already_cancelled",
                    deposit_code=deposit.code,
                )
            if deposit.status != "failed" or deposit.failure_reason != "expired":
                return ManualDepositCancellationResult(
                    "invalid_status",
                    deposit_code=deposit.code,
                )

            existing_credit = await session.scalar(
                select(PaymentTransaction.id).where(
                    PaymentTransaction.deposit_id == deposit.id,
                    PaymentTransaction.credit_status == "credited",
                )
            )
            existing_ledger = await session.scalar(
                select(WalletTransaction.id).where(
                    WalletTransaction.event_key == f"deposit:{deposit.id}"
                )
            )
            if existing_credit is not None or existing_ledger is not None:
                return ManualDepositCancellationResult(
                    "already_credited",
                    deposit_code=deposit.code,
                )

            user = await session.scalar(
                select(User).where(User.telegram_id == deposit.user_id).with_for_update()
            )
            if user is None:
                return ManualDepositCancellationResult(
                    "user_not_found",
                    deposit_code=deposit.code,
                )

            cancelled_at = datetime.now(UTC)
            deposit.status = "failed"
            deposit.failed_at = cancelled_at
            deposit.failure_reason = "admin_cancelled"
            return ManualDepositCancellationResult(
                "cancelled",
                user_id=user.telegram_id,
                amount=int(deposit.requested_amount),
                balance=int(user.balance),
                deposit_code=deposit.code,
                username=user.username,
                language=user.language,
            )


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
                source_row = (
                    await session.execute(
                        select(
                            Product.fulfillment_source,
                            Product.supplier_product_id,
                        )
                        .join(Deposit, Deposit.product_id == Product.id)
                        .where(
                            Deposit.code == deposit_code,
                            Deposit.payment_kind == "direct_purchase",
                            Product.fulfillment_source.in_(
                                EXTERNAL_FULFILLMENT_SOURCES
                            ),
                        )
                    )
                ).one_or_none()
            supplier_source = str(source_row[0]) if source_row is not None else ""
            supplier_product_id = source_row[1] if source_row is not None else None
            external_clients: list[ExternalSupplierClient] = []
            external_client = supplier_client_for_source(
                supplier_source,
                supplier_client,
                lehai_client,
            )
            if external_client is not None:
                external_clients.append(external_client)
            if (
                is_multi_supplier_product(supplier_source, supplier_product_id)
                and lehai_client is not None
                and lehai_client not in external_clients
            ):
                external_clients.append(lehai_client)
            if external_clients:
                unique_clients = {
                    id(client): client for client in external_clients
                }.values()
                ordered_clients = sorted(
                    unique_clients,
                    key=lambda client: (
                        0
                        if getattr(client, "provider", "") == "sumistore"
                        else 1,
                        getattr(client, "provider", ""),
                    ),
                )
                async with AsyncExitStack() as stack:
                    for client in ordered_clients:
                        await stack.enter_async_context(supplier_balance_guard(client))
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
            manual_credit_exists = None
            if deposit.status == "paid" and amount == deposit.requested_amount:
                manual_credit_exists = await session.scalar(
                    select(PaymentTransaction.id).where(
                        PaymentTransaction.deposit_id == deposit.id,
                        PaymentTransaction.provider_tx_id == f"ADMIN-DEPOSIT-{deposit.id}",
                        PaymentTransaction.credit_status == "credited",
                    )
                )
            if manual_credit_exists is not None:
                rejected_status = "manual_approval_matched"
                credit_status = "manual_matched"
            elif now >= expires_at:
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
                if credit_status in {"expired", "failed_request"}:
                    await release_deposit_flash_sale(session, deposit)
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
                product = await session.scalar(
                    select(Product)
                    .where(Product.id == deposit.product_id)
                    .with_for_update()
                )
                reserved_coupon: DiscountCode | None = None
                coupon_can_fulfill = True
                if deposit.discount_code_id is not None:
                    reserved_coupon = await session.scalar(
                        select(DiscountCode)
                        .where(DiscountCode.id == deposit.discount_code_id)
                        .with_for_update()
                    )
                    previous_coupon_order = await session.scalar(
                        select(Order.id)
                        .where(
                            Order.user_id == user.telegram_id,
                            Order.discount_code_id == deposit.discount_code_id,
                        )
                        .limit(1)
                    )
                    coupon_can_fulfill = bool(
                        reserved_coupon is not None
                        and reserved_coupon.product_id == deposit.product_id
                        and previous_coupon_order is None
                        and (
                            reserved_coupon.max_uses <= 0
                            or reserved_coupon.used_count < reserved_coupon.max_uses
                        )
                    )
                flash_sale_can_fulfill = True
                deposit_campaign: FlashSaleCampaign | None = None
                if deposit.flash_sale_id is not None:
                    deposit_campaign = await session.scalar(
                        select(FlashSaleCampaign)
                        .where(FlashSaleCampaign.id == deposit.flash_sale_id)
                        .with_for_update()
                    )
                    unsafe_status = (
                        stop_unsafe_flash_sale(deposit_campaign, product)
                        if deposit_campaign is not None and product is not None
                        else None
                    )
                    flash_sale_can_fulfill = bool(
                        deposit_campaign is not None
                        and unsafe_status is None
                        and deposit_campaign.status
                        not in {"cost_exceeded", "price_invalid"}
                        and (
                            product is None
                            or product.fulfillment_source
                            not in EXTERNAL_FULFILLMENT_SOURCES
                            or int(product.supplier_price or 0)
                            <= deposit_campaign.sale_price
                        )
                        and (
                            product is None
                            or deposit_campaign.sale_price < product.price
                        )
                    )
                items: list[InventoryItem] = []
                supplier_purchase_made = False
                supplier_purchase_parts: tuple[
                    tuple[SupplierPurchase, int], ...
                ] = ()
                item_sale_prices: dict[int, int] = {}
                item_discounts: dict[int, int] = {}
                if (
                    product is not None
                    and product.active
                    and not product.force_out_of_stock
                    and coupon_can_fulfill
                    and flash_sale_can_fulfill
                    and deposit.inventory_price_locked == product.price_lock_enabled
                ):
                    if deposit.quantity == 1 or product.allow_quantity:
                        external_client = supplier_client_for_source(
                            product.fulfillment_source,
                            supplier_client,
                            lehai_client,
                        )
                        if product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES:
                            supplier_stock = 0
                            multi_route_fetch: SupplierRouteFetch | None = None
                            multi_plan: tuple[tuple[SupplierRoute, int], ...] = ()
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
                                if (
                                    is_multi_supplier_product(
                                        product.fulfillment_source,
                                        product.supplier_product_id,
                                    )
                                    and product.supplier_product_id
                                ):
                                    multi_route_fetch = (
                                        await fetch_sumistore_supplier_routes(
                                            product.supplier_product_id,
                                            supplier_client,
                                            lehai_client,
                                        )
                                    )
                                    await refresh_external_product(
                                        session,
                                        product,
                                        supplier_client,
                                        lehai_client=lehai_client,
                                        route_fetch=multi_route_fetch,
                                    )
                                    multi_plan = plan_supplier_routes(
                                        multi_route_fetch.routes,
                                        deposit.quantity,
                                    )
                                else:
                                    await refresh_product_from_supplier(
                                        session,
                                        product,
                                        supplier_client,
                                        lehai_client,
                                    )
                                if deposit_campaign is not None:
                                    unsafe_status = stop_unsafe_flash_sale(
                                        deposit_campaign,
                                        product,
                                    )
                                    flash_sale_can_fulfill = bool(
                                        unsafe_status is None
                                        and deposit_campaign.status
                                        not in {"cost_exceeded", "price_invalid"}
                                        and int(product.supplier_price or 0)
                                        <= deposit_campaign.sale_price
                                        and deposit_campaign.sale_price < product.price
                                    )
                                supplier_stock = max(
                                    0,
                                    product.external_stock - recovered_stock,
                                )
                            if not items and (
                                flash_sale_can_fulfill
                                and cipher is not None
                                and (
                                    external_client is not None
                                    or bool(multi_plan)
                                )
                                and product.supplier_product_id
                                and supplier_stock >= deposit.quantity
                            ):
                                multi_quote: MultiSupplierQuote | None = None
                                if multi_plan:
                                    tier = await session.scalar(
                                        select(QuantityDiscount)
                                        .where(
                                            QuantityDiscount.product_id == product.id,
                                            QuantityDiscount.active.is_(True),
                                            QuantityDiscount.min_quantity
                                            <= deposit.quantity,
                                        )
                                        .order_by(
                                            QuantityDiscount.min_quantity.desc(),
                                            QuantityDiscount.discount_percent.desc(),
                                        )
                                        .limit(1)
                                    )
                                    locked_pricing = ProductPricing(
                                        original_unit_price=product.price,
                                        discount_per_unit=0,
                                        final_unit_price=product.price,
                                        coupon=reserved_coupon,
                                        quantity_discount_percent=(
                                            tier.discount_percent if tier is not None else 0
                                        ),
                                        flash_sale=deposit_campaign,
                                    )
                                    multi_quote = price_supplier_plan(
                                        product,
                                        multi_plan,
                                        locked_pricing,
                                    )
                                    quote_is_safe = (
                                        multi_quote.total_amount
                                        == deposit.requested_amount
                                        and not any(
                                            allocation.route.snapshot.unit_price
                                            > allocation.final_unit_price
                                            for allocation in multi_quote.allocations
                                        )
                                    )
                                    if not quote_is_safe:
                                        multi_plan = ()
                                try:
                                    if multi_plan:
                                        supplier_purchase_parts = (
                                            await execute_supplier_route_plan(
                                                session,
                                                product,
                                                multi_plan,
                                                request_key=f"qr-{deposit.code}",
                                                cipher=cipher,
                                            )
                                        )
                                    elif multi_route_fetch is not None:
                                        supplier_purchase_parts = ()
                                    else:
                                        supplier_purchase = await buy_supplier_product(
                                            session,
                                            external_client,
                                            product.supplier_product_id,
                                            deposit.quantity,
                                            idempotency_key=f"qr-{deposit.code}",
                                            shop_product_id=product.id,
                                        )
                                        supplier_purchase_parts = (
                                            (
                                                supplier_purchase,
                                                max(
                                                    0,
                                                    supplier_purchase.unit_price
                                                    or product.supplier_price
                                                    or 0,
                                                ),
                                            ),
                                        )
                                except SupplierError:
                                    if not multi_plan:
                                        product.external_stock = recovered_stock
                                        purchase_is_blocked = getattr(
                                            external_client,
                                            "purchase_is_blocked",
                                            None,
                                        )
                                        if callable(
                                            purchase_is_blocked
                                        ) and purchase_is_blocked(
                                            product.supplier_product_id
                                        ):
                                            await apply_supplier_stock(
                                                session,
                                                product,
                                                0,
                                                notify_on_increase=False,
                                                local_inventory_stock=recovered_stock,
                                            )
                                else:
                                    now = datetime.now(UTC)
                                    if (
                                        not multi_plan
                                        and supplier_purchase_parts
                                        and supplier_purchase.unit_price > 0
                                        and (
                                        supplier_purchase.provider == "sumistore"
                                        or (
                                            deposit_campaign is not None
                                            and supplier_purchase.unit_price
                                            > deposit_campaign.sale_price
                                        )
                                        )
                                    ):
                                        await apply_supplier_price(
                                            session,
                                            product,
                                            supplier_purchase.unit_price,
                                        )
                                    if (
                                        multi_plan
                                        and any(
                                            unit_cost
                                            > allocation.route.snapshot.unit_price
                                            for (_, unit_cost), allocation in zip(
                                                supplier_purchase_parts,
                                                multi_quote.allocations,
                                                strict=True,
                                            )
                                        )
                                    ):
                                        await preserve_supplier_purchase_parts(
                                            session,
                                            product,
                                            supplier_purchase_parts,
                                            cipher,
                                        )
                                        flash_sale_can_fulfill = False
                                        logger.warning(
                                            "Multi-supplier QR cost changed; accounts moved to "
                                            "inventory: deposit=%s",
                                            deposit.code,
                                        )
                                        supplier_purchase_parts = ()
                                    elif supplier_purchase_parts:
                                        supplier_unit_cost = supplier_purchase_parts[0][1]
                                        if (
                                            not multi_plan
                                            and deposit_campaign is not None
                                            and supplier_unit_cost
                                            > deposit_campaign.sale_price
                                        ):
                                            recovery_code = await preserve_supplier_purchase_for_resale(
                                                session,
                                                product,
                                                supplier_purchase,
                                                cipher,
                                                supplier_unit_cost,
                                            )
                                            flash_sale_can_fulfill = False
                                            logger.warning(
                                                "Flash QR purchase moved to inventory after "
                                                "supplier cost increase: campaign=%s deposit=%s "
                                                "cost=%s recovery=%s",
                                                deposit_campaign.id,
                                                deposit.code,
                                                supplier_unit_cost,
                                                recovery_code,
                                            )
                                            supplier_purchase_parts = ()
                                    if supplier_purchase_parts:
                                        product.external_stock = max(
                                            0,
                                            product.external_stock - deposit.quantity,
                                        )
                                        for position, (purchase, unit_cost) in enumerate(
                                            supplier_purchase_parts
                                        ):
                                            allocation = (
                                                multi_quote.allocations[position]
                                                if multi_plan
                                                else None
                                            )
                                            for item_index, secret_value in enumerate(
                                                purchase.accounts
                                            ):
                                                item = InventoryItem(
                                                    product_id=product.id,
                                                    encrypted_secret=cipher.encrypt(
                                                        secret_value
                                                    ),
                                                    cost_amount=unit_cost,
                                                    supplier_order_code=(
                                                        purchase.order_code or None
                                                    ),
                                                    supplier_item_index=item_index,
                                                    status="sold",
                                                    sold_at=now,
                                                )
                                                session.add(item)
                                                await session.flush()
                                                items.append(item)
                                                if allocation is not None:
                                                    item_sale_prices[item.id] = (
                                                        allocation.final_unit_price
                                                    )
                                                    item_discounts[item.id] = (
                                                        allocation.discount_per_unit
                                                    )
                                        supplier_purchase_made = True
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
                            amount=item_sale_prices.get(
                                item.id,
                                deposit.requested_amount // deposit.quantity,
                            ),
                            cost_amount=item.cost_amount,
                            discount_amount=item_discounts.get(
                                item.id,
                                deposit.discount_amount // deposit.quantity,
                            ),
                            discount_code_id=deposit.discount_code_id,
                            discount_code=deposit.discount_code,
                            flash_sale_id=deposit.flash_sale_id,
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
                        for purchase, unit_cost in supplier_purchase_parts:
                            record_supplier_purchase(
                                session,
                                amount=unit_cost * len(purchase.accounts),
                                supplier_order_code=purchase.order_code or None,
                                shop_order_code=batch_code,
                                product_id=product.id,
                                quantity=len(purchase.accounts),
                                provider=purchase.provider,
                            )
                    if reserved_coupon is not None:
                        reserved_coupon.used_count += 1
                    await award_referral_commission(
                        session,
                        user,
                        shop_order_code=batch_code,
                        order_amount=amount,
                        sales_channel="telegram",
                        commission_percent=referral_commission_percent,
                    )
                    await complete_deposit_flash_sale(session, deposit)
                    if product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES:
                        await release_price_lock_if_inventory_empty(session, product)
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

            await release_deposit_flash_sale(session, deposit)
            is_direct_fallback = deposit.payment_kind == "direct_purchase"
            apply_wallet_change(
                session,
                user,
                amount,
                kind="direct_purchase_fallback" if is_direct_fallback else "deposit",
                event_key=f"deposit:{deposit.id}",
                reference_type="deposit",
                reference_id=deposit.code,
                description=(
                    f"Thanh toán trực tiếp {deposit.code} không giao được, đã chuyển vào ví"
                    if is_direct_fallback
                    else f"Nạp tiền vào ví qua mã {deposit.code}"
                ),
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
            user_id = user.telegram_id
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
