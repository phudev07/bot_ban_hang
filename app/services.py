import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime

from aiogram.types import User as TelegramUser
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.models import (
    Category,
    Deposit,
    DiscountCode,
    InventoryItem,
    Order,
    PaymentTransaction,
    Product,
    User,
)
from app.suppliers import (
    SumistoreClient,
    SupplierError,
    refresh_external_product,
)
from app.utils import SecretCipher, find_deposit_code


async def ensure_user(session: AsyncSession, telegram_user: TelegramUser) -> User:
    user = await session.get(User, telegram_user.id)
    if user is None:
        user = User(
            telegram_id=telegram_user.id,
            full_name=telegram_user.full_name,
            username=telegram_user.username,
        )
        session.add(user)
        await session.flush()
    else:
        user.full_name = telegram_user.full_name
        user.username = telegram_user.username
    return user


async def available_stock(
    session: AsyncSession,
    product_id: int,
    supplier_client: SumistoreClient | None = None,
    *,
    refresh_external: bool = False,
) -> int:
    product = await session.get(Product, product_id)
    if product is None:
        return 0
    if product.fulfillment_source == "sumistore":
        if refresh_external:
            await refresh_external_product(session, product, supplier_client)
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


async def product_pricing(
    session: AsyncSession,
    product: Product,
    *,
    coupon_code: str | None = None,
    coupon_id: int | None = None,
    lock_coupon: bool = False,
) -> ProductPricing | None:
    if not coupon_code and coupon_id is None:
        return ProductPricing(product.price, 0, product.price)

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
        discount = product.price * coupon.discount_value // 100
    else:
        discount = coupon.discount_value
    discount = max(0, min(discount, max(0, product.price - 1)))
    return ProductPricing(
        original_unit_price=product.price,
        discount_per_unit=discount,
        final_unit_price=product.price - discount,
        coupon=coupon,
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
    deposit_count = int(
        await session.scalar(
            select(func.count(PaymentTransaction.id)).where(
                PaymentTransaction.user_id == user_id
            )
        )
        or 0
    )
    total_deposited = int(
        await session.scalar(
            select(func.coalesce(func.sum(PaymentTransaction.amount), 0)).where(
                PaymentTransaction.user_id == user_id
            )
        )
        or 0
    )
    return UserActivityStats(
        purchase_count=batch_purchases + single_purchases,
        purchased_items=purchased_items,
        deposit_count=deposit_count,
        total_spent=total_spent,
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
    coupon_code: str | None = None,
    coupon_id: int | None = None,
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
            if product.fulfillment_source == "sumistore":
                await refresh_external_product(session, product, supplier_client)
                if (
                    supplier_client is None
                    or not product.supplier_product_id
                    or product.external_stock < quantity
                ):
                    return PurchaseResult(False, "out_of_stock")
            pricing = await product_pricing(
                session,
                product,
                coupon_code=coupon_code,
                coupon_id=coupon_id,
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
                )

            if product.fulfillment_source == "sumistore":
                sale_unit_price = pricing.final_unit_price
                try:
                    supplier_purchase = await supplier_client.buy(
                        product.supplier_product_id,
                        quantity,
                    )
                except SupplierError as exc:
                    product.external_stock = 0
                    if exc.code in {"INSUFFICIENT_BALANCE", "INSUFFICIENT_STOCK"}:
                        return PurchaseResult(False, "out_of_stock")
                    return PurchaseResult(False, "supplier_unavailable")

                now = datetime.now(UTC)
                batch_code = f"B{secrets.token_hex(5).upper()}"
                if supplier_purchase.unit_price > 0:
                    product.supplier_price = supplier_purchase.unit_price
                    product.price = supplier_purchase.unit_price + product.supplier_markup
                cost_unit_price = max(
                    0,
                    supplier_purchase.unit_price
                    or product.supplier_price
                    or 0,
                )
                product.external_stock = max(0, product.external_stock - quantity)
                orders = []
                secret_values = []
                for secret_value in supplier_purchase.accounts:
                    item = InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt(secret_value),
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
                        status="completed",
                        delivered_at=now,
                        product=product,
                        inventory_item=item,
                    )
                    session.add(order)
                    orders.append(order)
                    secret_values.append(secret_value)
                if pricing.coupon is not None:
                    pricing.coupon.used_count += 1
                user.balance -= total_amount
                await session.flush()
                return PurchaseResult(
                    True,
                    "completed",
                    orders=orders,
                    secrets=secret_values,
                    total_amount=total_amount,
                    discount_amount=total_discount,
                    coupon_code=pricing.coupon.code if pricing.coupon else None,
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
                    cost_amount=0,
                    discount_amount=pricing.discount_per_unit,
                    discount_code_id=pricing.coupon.id if pricing.coupon else None,
                    discount_code=pricing.coupon.code if pricing.coupon else None,
                    batch_code=batch_code,
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
            await session.flush()
            return PurchaseResult(
                True,
                "completed",
                orders=orders,
                secrets=secret_values,
                total_amount=total_amount,
                discount_amount=total_discount,
                coupon_code=pricing.coupon.code if pricing.coupon else None,
            )


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
) -> Deposit:
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
            existing = await session.scalar(
                select(PaymentTransaction).where(
                    PaymentTransaction.provider_tx_id == provider_tx_id
                )
            )
            if existing is not None:
                return PaymentResult("duplicate", existing.user_id, existing.amount)

            deposit = await session.scalar(
                select(Deposit).where(Deposit.code == deposit_code).with_for_update()
            )
            if deposit is None:
                return PaymentResult("deposit_not_found")
            user = await session.scalar(
                select(User).where(User.telegram_id == deposit.user_id).with_for_update()
            )
            if user is None:
                return PaymentResult("user_not_found")

            direct_purchase_ready = (
                deposit.payment_kind == "direct_purchase"
                and deposit.status == "pending"
                and deposit.product_id is not None
                and amount == deposit.requested_amount
                and not user.is_blocked
                and deposit.quantity >= 1
            )
            if direct_purchase_ready:
                product = await session.get(Product, deposit.product_id)
                items: list[InventoryItem] = []
                supplier_order_code: str | None = None
                supplier_unit_cost = 0
                if product is not None and product.active:
                    if deposit.quantity == 1 or product.allow_quantity:
                        if product.fulfillment_source == "sumistore":
                            await refresh_external_product(session, product, supplier_client)
                            if (
                                cipher is not None
                                and supplier_client is not None
                                and product.supplier_product_id
                                and product.external_stock >= deposit.quantity
                            ):
                                try:
                                    supplier_purchase = await supplier_client.buy(
                                        product.supplier_product_id,
                                        deposit.quantity,
                                    )
                                except SupplierError:
                                    product.external_stock = 0
                                else:
                                    now = datetime.now(UTC)
                                    supplier_order_code = supplier_purchase.order_code or None
                                    supplier_unit_cost = max(
                                        0,
                                        supplier_purchase.unit_price
                                        or product.supplier_price
                                        or 0,
                                    )
                                    if supplier_purchase.unit_price > 0:
                                        product.supplier_price = supplier_purchase.unit_price
                                        product.price = (
                                            supplier_purchase.unit_price + product.supplier_markup
                                        )
                                    product.external_stock = max(
                                        0,
                                        product.external_stock - deposit.quantity,
                                    )
                                    items = [
                                        InventoryItem(
                                            product_id=product.id,
                                            encrypted_secret=cipher.encrypt(secret_value),
                                            status="sold",
                                            sold_at=now,
                                        )
                                        for secret_value in supplier_purchase.accounts
                                    ]
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
                    now = datetime.now(UTC)
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
                            cost_amount=(
                                supplier_unit_cost
                                if product.fulfillment_source == "sumistore"
                                else 0
                            ),
                            discount_amount=deposit.discount_amount // deposit.quantity,
                            discount_code_id=deposit.discount_code_id,
                            discount_code=deposit.discount_code,
                            batch_code=batch_code,
                            supplier_order_code=supplier_order_code,
                            status="completed",
                            delivered_at=now,
                        )
                        session.add(order)
                        orders.append(order)
                    if deposit.discount_code_id is not None:
                        coupon = await session.scalar(
                            select(DiscountCode)
                            .where(DiscountCode.id == deposit.discount_code_id)
                            .with_for_update()
                        )
                        if coupon is not None:
                            coupon.used_count += 1
                    deposit.status = "paid"
                    deposit.paid_amount = (deposit.paid_amount or 0) + amount
                    deposit.paid_at = now
                    session.add(
                        PaymentTransaction(
                            deposit_id=deposit.id,
                            user_id=user.telegram_id,
                            provider_tx_id=provider_tx_id,
                            amount=amount,
                        )
                    )
                    await session.flush()
                    return PaymentResult(
                        "direct_purchase_completed",
                        user.telegram_id,
                        amount,
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
            deposit.paid_amount = (deposit.paid_amount or 0) + amount
            deposit.paid_at = datetime.now(UTC)
            session.add(
                PaymentTransaction(
                    deposit_id=deposit.id,
                    user_id=user.telegram_id,
                    provider_tx_id=provider_tx_id,
                    amount=amount,
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
                & Product.fulfillment_source.in_(("local", "sumistore"))
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
            Product.fulfillment_source.in_(("local", "sumistore")),
            Product.product_type == "account",
        )
        .order_by(Product.id)
    )
    if category_id is not None:
        statement = statement.where(Product.category_id == category_id)
    result = await session.scalars(statement)
    return list(result)
