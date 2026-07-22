import asyncio
from datetime import UTC, datetime, timedelta
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import (
    BalanceAdjustment,
    Category,
    Deposit,
    DiscountCode,
    InventoryItem,
    Order,
    PaymentTransaction,
    Product,
    QuantityDiscount,
    ReferralReward,
    SupplierRecoveryRequest,
    User,
    WalletTransaction,
)
from app.services import (
    approve_wallet_deposit,
    available_stock,
    cancel_wallet_deposit,
    CouponValidationError,
    create_deposit,
    order_bundle,
    process_sepay_payment,
    product_pricing,
    purchase_quantity_limit,
    purchase_product,
    recent_orders,
    user_activity_stats,
)
from app.suppliers import SupplierError, SupplierPurchase, SupplierSnapshot
from app.utils import SecretCipher


async def make_database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_purchase_quantity_limit_never_exceeds_current_stock() -> None:
    product = Product(max_quantity=100)

    assert purchase_quantity_limit(product, 24) == 24
    assert purchase_quantity_limit(product, 150) == 100
    assert purchase_quantity_limit(product, 0) == 0


class FakeSupplier:
    def __init__(self, *, balance: int = 100_000, stock: int = 100) -> None:
        self.balance = balance
        self.stock = stock
        self.buy_calls = 0

    async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
        return SupplierSnapshot(
            product_id=product_id,
            name="ChatGPT Plus",
            description="Supplier product",
            unit_price=15_000,
            source_stock=self.stock,
            owner_balance=self.balance,
        )

    async def buy(self, product_id: str, quantity: int) -> SupplierPurchase:
        self.buy_calls += 1
        return SupplierPurchase(
            order_code="API-TELE-TEST123",
            unit_price=15_000,
            accounts=tuple(f"chatgpt{index}:password" for index in range(1, quantity + 1)),
        )


def test_external_stock_ui_refresh_uses_short_cache() -> None:
    class CountingSupplier:
        provider = "sumistore"

        def __init__(self) -> None:
            self.calls = 0

        async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
            self.calls += 1
            return SupplierSnapshot(
                product_id=product_id,
                name="Cached product",
                description="",
                unit_price=15_000,
                source_stock=8,
                owner_balance=150_000,
            )

    async def scenario() -> None:
        engine, sessions = await make_database()
        supplier = CountingSupplier()
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Cached product",
                name_en="Cached product",
                price=20_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-CACHED",
                supplier_price=15_000,
                supplier_markup=5_000,
                external_stock=7,
                supplier_synced_at=datetime.now(UTC),
            )
            session.add(product)
            await session.commit()
            product_id = product.id

        async with sessions() as session:
            assert (
                await available_stock(
                    session,
                    product_id,
                    supplier,  # type: ignore[arg-type]
                    refresh_external=True,
                    refresh_max_age_seconds=10,
                )
                == 7
            )
            assert supplier.calls == 0
            product = await session.get(Product, product_id)
            assert product is not None
            product.supplier_synced_at = datetime.now(UTC) - timedelta(seconds=11)
            await session.commit()

        async with sessions() as session:
            assert (
                await available_stock(
                    session,
                    product_id,
                    supplier,  # type: ignore[arg-type]
                    refresh_external=True,
                    refresh_max_age_seconds=10,
                )
                == 8
            )
            assert (
                await available_stock(
                    session,
                    product_id,
                    supplier,  # type: ignore[arg-type]
                    refresh_external=True,
                    refresh_max_age_seconds=10,
                )
                == 8
            )
            assert supplier.calls == 1
        await engine.dispose()

    asyncio.run(scenario())


class TimeoutRecoveringSupplier(FakeSupplier):
    async def buy(self, product_id: str, quantity: int) -> SupplierPurchase:
        self.buy_calls += 1
        raise SupplierError("SUPPLIER_UNAVAILABLE")

    async def recover_recent_purchase(
        self,
        product_id: str,
        quantity: int,
        **_kwargs,
    ) -> SupplierPurchase:
        return SupplierPurchase(
            order_code="API-TELE-RECOVERED",
            unit_price=15_000,
            accounts=tuple(
                f"recovered{index}:password" for index in range(1, quantity + 1)
            ),
            product_id=product_id,
        )


class PendingRecoverySupplier(FakeSupplier):
    provider = "sumistore"

    async def buy(self, product_id: str, quantity: int) -> SupplierPurchase:
        self.buy_calls += 1
        raise SupplierError("SUPPLIER_UNAVAILABLE")

    async def recover_recent_purchase(
        self,
        product_id: str,
        quantity: int,
        **_kwargs,
    ) -> None:
        return None


def test_purchase_is_atomic_and_delivers_stock() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Item",
                name_en="Item",
                price=50_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=100_000)
            session.add_all([product, user])
            await session.flush()
            item = InventoryItem(
                product_id=product.id,
                encrypted_secret=cipher.encrypt("account:password"),
            )
            session.add(item)
            await session.commit()

            result = await purchase_product(sessions, user.telegram_id, product.id, cipher)
            assert result.ok is True
            assert result.secret == "account:password"

            await session.refresh(user)
            await session.refresh(item)
            assert user.balance == 50_000
            assert item.status == "sold"

            second = await purchase_product(sessions, user.telegram_id, product.id, cipher)
            assert second.ok is False
            assert second.message == "out_of_stock"
        await engine.dispose()

    asyncio.run(scenario())


def test_coupon_validation_reports_the_exact_failure_reason() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        now = datetime.now(UTC)
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="San pham A",
                name_en="Product A",
                price=50_000,
            )
            other_product = Product(
                category_id=category.id,
                name_vi="San pham B",
                name_en="Product B",
                price=50_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=100_000)
            session.add_all([product, other_product, user])
            await session.flush()
            coupons = [
                DiscountCode(
                    product_id=other_product.id,
                    code="WRONGPRODUCT",
                    discount_type="fixed",
                    discount_value=5_000,
                ),
                DiscountCode(
                    product_id=product.id,
                    code="INACTIVE",
                    discount_type="fixed",
                    discount_value=5_000,
                    active=False,
                ),
                DiscountCode(
                    product_id=product.id,
                    code="FUTURE",
                    discount_type="fixed",
                    discount_value=5_000,
                    starts_at=now + timedelta(days=1),
                ),
                DiscountCode(
                    product_id=product.id,
                    code="EXPIRED",
                    discount_type="fixed",
                    discount_value=5_000,
                    expires_at=now - timedelta(days=1),
                ),
                DiscountCode(
                    product_id=product.id,
                    code="EXHAUSTED",
                    discount_type="fixed",
                    discount_value=5_000,
                    max_uses=1,
                    used_count=1,
                ),
                DiscountCode(
                    product_id=product.id,
                    code="USEDONCE",
                    discount_type="fixed",
                    discount_value=5_000,
                    max_uses=10,
                    used_count=1,
                ),
            ]
            session.add_all(coupons)
            await session.flush()
            used_coupon = coupons[-1]
            item = InventoryItem(product_id=product.id, encrypted_secret="unused")
            session.add(item)
            await session.flush()
            session.add(
                Order(
                    user_id=user.telegram_id,
                    product_id=product.id,
                    inventory_item_id=item.id,
                    amount=45_000,
                    discount_code_id=used_coupon.id,
                    discount_code=used_coupon.code,
                    status="completed",
                )
            )
            await session.commit()

        expected_errors = {
            "": "coupon_empty",
            "MISSING": "coupon_not_found",
            "WRONGPRODUCT": "coupon_wrong_product",
            "INACTIVE": "coupon_inactive",
            "FUTURE": "coupon_not_started",
            "EXPIRED": "coupon_expired",
            "EXHAUSTED": "coupon_exhausted",
            "USEDONCE": "coupon_already_used",
        }
        async with sessions() as session:
            product = await session.scalar(select(Product).where(Product.name_en == "Product A"))
            assert product is not None
            for code, expected_error in expected_errors.items():
                try:
                    await product_pricing(
                        session,
                        product,
                        coupon_code=code,
                        user_id=123456,
                        raise_coupon_error=True,
                    )
                except CouponValidationError as exc:
                    assert exc.code == expected_error
                else:
                    raise AssertionError(f"Expected coupon error {expected_error}")
        await engine.dispose()

    asyncio.run(scenario())


def test_product_coupon_reduces_each_item_and_tracks_usage() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tài khoản",
                name_en="Account",
                price=50_000,
                allow_quantity=True,
                max_quantity=10,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=100_000)
            session.add_all([product, user])
            await session.flush()
            coupon = DiscountCode(
                product_id=product.id,
                code="SAVE5K",
                discount_type="fixed",
                discount_value=5_000,
                max_uses=10,
            )
            session.add(coupon)
            session.add_all(
                [
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt(f"account{index}:password"),
                    )
                    for index in (1, 2)
                ]
            )
            await session.commit()

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            quantity=2,
            coupon_code="save5k",
        )
        assert result.ok is True
        assert result.total_amount == 90_000
        assert result.discount_amount == 10_000
        assert result.coupon_code == "SAVE5K"

        repeated = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            coupon_code="SAVE5K",
        )
        assert repeated.ok is False
        assert repeated.message == "coupon_already_used"

        async with sessions() as session:
            user = await session.get(User, user.telegram_id)
            coupon = await session.scalar(select(DiscountCode))
            orders = list(await session.scalars(select(Order).order_by(Order.id)))
            assert user is not None and user.balance == 10_000
            assert coupon is not None and coupon.used_count == 1
            assert all(order.amount == 45_000 for order in orders)
            assert all(order.discount_amount == 5_000 for order in orders)
            assert all(order.discount_code == "SAVE5K" for order in orders)
            assert all(order.cost_amount == 0 for order in orders)
        await engine.dispose()

    asyncio.run(scenario())


def test_quantity_discount_uses_highest_tier_and_stacks_with_coupon() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tai khoan so luong lon",
                name_en="Bulk account",
                price=50_000,
                allow_quantity=True,
                max_quantity=20,
            )
            user = User(telegram_id=654321, full_name="Bulk buyer", balance=1_000_000)
            session.add_all([product, user])
            await session.flush()
            coupon = DiscountCode(
                product_id=product.id,
                code="BULK5K",
                discount_type="fixed",
                discount_value=5_000,
                max_uses=5,
            )
            session.add_all(
                [
                    coupon,
                    QuantityDiscount(
                        product_id=product.id,
                        min_quantity=5,
                        discount_percent=5,
                    ),
                    QuantityDiscount(
                        product_id=product.id,
                        min_quantity=10,
                        discount_percent=10,
                    ),
                ]
            )
            session.add_all(
                [
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt(f"bulk{index}:password"),
                    )
                    for index in range(1, 11)
                ]
            )
            await session.commit()

            lower_tier = await product_pricing(session, product, quantity=6)
            assert lower_tier is not None
            assert lower_tier.quantity_discount_percent == 5
            assert lower_tier.final_unit_price == 47_500

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            quantity=10,
            coupon_code="bulk5k",
        )
        assert result.ok is True
        assert result.total_amount == 400_000
        assert result.discount_amount == 100_000
        assert result.coupon_code == "BULK5K"
        assert result.quantity_discount_percent == 10

        async with sessions() as session:
            stored_user = await session.get(User, user.telegram_id)
            stored_coupon = await session.scalar(select(DiscountCode))
            orders = list(await session.scalars(select(Order).order_by(Order.id)))
            assert stored_user is not None and stored_user.balance == 600_000
            assert stored_coupon is not None and stored_coupon.used_count == 1
            assert len(orders) == 10
            assert all(order.amount == 40_000 for order in orders)
            assert all(order.discount_amount == 10_000 for order in orders)
            assert all(order.discount_code == "BULK5K" for order in orders)
        await engine.dispose()

    asyncio.run(scenario())


def test_user_activity_counts_purchase_batches_and_deposits() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tài khoản",
                name_en="Account",
                price=50_000,
                allow_quantity=True,
                max_quantity=10,
            )
            user = User(telegram_id=55555, full_name="Buyer", balance=200_000)
            session.add_all([product, user])
            await session.flush()
            session.add_all(
                [
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt(f"account{index}:password"),
                    )
                    for index in (1, 2)
                ]
            )
            await session.commit()

        purchase = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            quantity=2,
        )
        assert purchase.ok is True

        async with sessions() as session:
            deposit = Deposit(
                user_id=user.telegram_id,
                code="NAP55555ABCD",
                requested_amount=20_000,
            )
            session.add(deposit)
            await session.commit()

        await process_sepay_payment(
            sessions,
            {
                "id": 44444,
                "transferType": "in",
                "transferAmount": 20_000,
                "content": "NAP55555ABCD",
            },
        )

        async with sessions() as session:
            stats = await user_activity_stats(session, user.telegram_id)
            bundled = await order_bundle(session, user.telegram_id, purchase.orders[0].id)
            history = await recent_orders(session, user.telegram_id, limit=1)
            assert stats.purchase_count == 1
            assert stats.purchased_items == 2
            assert stats.deposit_count == 1
            assert stats.total_spent == 100_000
            assert stats.total_deposited == 20_000
            assert len(bundled) == 2
            assert len(history) == 2
            assert {order.shop_order_code for order in history} == {
                purchase.orders[0].shop_order_code
            }
        await engine.dispose()

    asyncio.run(scenario())


def test_sepay_payment_is_idempotent() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            user = User(telegram_id=123456, full_name="Buyer", balance=0)
            deposit = Deposit(
                user_id=user.telegram_id,
                code="NAP123456ABCD",
                requested_amount=100_000,
            )
            session.add_all([user, deposit])
            await session.commit()

        payload = {
            "id": 98765,
            "transferType": "in",
            "transferAmount": 100_000,
            "content": "NAP123456ABCD",
        }
        first = await process_sepay_payment(sessions, payload)
        second = await process_sepay_payment(sessions, payload)
        another_transfer = await process_sepay_payment(sessions, {**payload, "id": 98766})
        assert first.status == "credited"
        assert first.balance == 100_000
        assert first.deposit_code == "NAP123456ABCD"
        assert first.paid_at is not None
        assert second.status == "duplicate"
        assert another_transfer.status == "already_paid_payment"

        async with sessions() as session:
            user = await session.get(User, 123456)
            wallet_transactions = list(
                await session.scalars(select(WalletTransaction).order_by(WalletTransaction.id))
            )
            assert user is not None
            assert user.balance == 100_000
            assert len(wallet_transactions) == 1
            assert wallet_transactions[0].kind == "deposit"
            assert wallet_transactions[0].amount == 100_000
            assert wallet_transactions[0].balance_before == 0
            assert wallet_transactions[0].balance_after == 100_000
        await engine.dispose()

    asyncio.run(scenario())


def test_manual_deposit_approval_credits_once_and_late_webhook_only_matches() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            user = User(telegram_id=321654, full_name="Manual approval buyer", balance=5_000)
            deposit = Deposit(
                user_id=user.telegram_id,
                code="NAP321654MANU",
                requested_amount=20_000,
                status="pending",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            session.add_all([user, deposit])
            await session.commit()
            deposit_id = deposit.id

        approved = await approve_wallet_deposit(
            sessions,
            deposit_id,
            admin_username="admin",
        )
        duplicate_approval = await approve_wallet_deposit(
            sessions,
            deposit_id,
            admin_username="admin",
        )
        webhook = await process_sepay_payment(
            sessions,
            {
                "id": "BANK-LATE-MANUAL",
                "transferType": "in",
                "transferAmount": 20_000,
                "content": "NAP321654MANU",
            },
        )

        assert approved.status == "approved"
        assert approved.balance == 25_000
        assert duplicate_approval.status == "already_paid"
        assert webhook.status == "manual_approval_matched"
        async with sessions() as session:
            user = await session.get(User, 321654)
            deposit = await session.get(Deposit, deposit_id)
            transactions = list(
                await session.scalars(
                    select(PaymentTransaction).order_by(PaymentTransaction.id)
                )
            )
            wallet_transactions = list(await session.scalars(select(WalletTransaction)))
            adjustments = list(await session.scalars(select(BalanceAdjustment)))
            assert user is not None and user.balance == 25_000
            assert deposit is not None and deposit.status == "paid"
            assert [item.credit_status for item in transactions] == [
                "credited",
                "manual_matched",
            ]
            assert len(wallet_transactions) == 1
            assert wallet_transactions[0].amount == 20_000
            assert wallet_transactions[0].balance_before == 5_000
            assert wallet_transactions[0].balance_after == 25_000
            assert len(adjustments) == 1
            assert adjustments[0].admin_username == "admin"
        await engine.dispose()

    asyncio.run(scenario())


def test_manual_deposit_cancellation_rejects_pending_requests() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            user = User(telegram_id=321655, full_name="Cancelled deposit buyer", balance=5_000)
            deposit = Deposit(
                user_id=user.telegram_id,
                code="NAP321655CANC",
                requested_amount=20_000,
                status="pending",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            session.add_all([user, deposit])
            await session.commit()
            deposit_id = deposit.id

        cancelled = await cancel_wallet_deposit(sessions, deposit_id)
        assert cancelled.status == "invalid_status"
        async with sessions() as session:
            user = await session.get(User, 321655)
            deposit = await session.get(Deposit, deposit_id)
            assert user is not None and user.balance == 5_000
            assert deposit is not None and deposit.status == "pending"
            assert deposit.failure_reason is None
            assert deposit.failed_at is None
            assert await session.scalar(select(PaymentTransaction.id)) is None
            assert await session.scalar(select(WalletTransaction.id)) is None
            assert await session.scalar(select(BalanceAdjustment.id)) is None

        webhook = await process_sepay_payment(
            sessions,
            {
                "id": "BANK-LATE-CANCELLED",
                "transferType": "in",
                "transferAmount": 20_000,
                "content": "NAP321655CANC",
            },
        )
        assert webhook.status == "credited"
        async with sessions() as session:
            user = await session.get(User, 321655)
            transaction = await session.scalar(select(PaymentTransaction))
            assert user is not None and user.balance == 25_000
            assert transaction is not None
            assert transaction.credit_status == "credited"
            wallet_transaction = await session.scalar(select(WalletTransaction))
            assert wallet_transaction is not None
            assert wallet_transaction.amount == 20_000
            assert await session.scalar(select(BalanceAdjustment.id)) is None
        await engine.dispose()

    asyncio.run(scenario())


def test_manual_deposit_cancellation_allows_expired_requests() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            user = User(telegram_id=321656, full_name="Expired deposit buyer", balance=5_000)
            deposit = Deposit(
                user_id=user.telegram_id,
                code="NAP321656EXPD",
                requested_amount=20_000,
                status="failed",
                failure_reason="expired",
                failed_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) - timedelta(minutes=1),
            )
            session.add_all([user, deposit])
            await session.commit()
            deposit_id = deposit.id

        cancelled = await cancel_wallet_deposit(sessions, deposit_id)
        assert cancelled.status == "cancelled"
        async with sessions() as session:
            user = await session.get(User, 321656)
            deposit = await session.get(Deposit, deposit_id)
            assert user is not None and user.balance == 5_000
            assert deposit is not None
            assert deposit.status == "failed"
            assert deposit.failure_reason == "admin_cancelled"
            assert await session.scalar(select(PaymentTransaction.id)) is None
            assert await session.scalar(select(WalletTransaction.id)) is None
        await engine.dispose()

    asyncio.run(scenario())


def test_direct_purchase_payment_delivers_without_using_wallet() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tài khoản",
                name_en="Account",
                price=50_000,
                allow_quantity=True,
                max_quantity=10,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=10_000)
            session.add_all([product, user])
            await session.flush()
            items = [
                InventoryItem(
                    product_id=product.id,
                    encrypted_secret=cipher.encrypt(f"account{index}:password"),
                )
                for index in (1, 2)
            ]
            deposit = Deposit(
                user_id=user.telegram_id,
                code="NAP123456ABCD",
                requested_amount=100_000,
                payment_kind="direct_purchase",
                product_id=product.id,
                quantity=2,
            )
            session.add_all([*items, deposit])
            await session.commit()

        payload = {
            "id": 22222,
            "transferType": "in",
            "transferAmount": 100_000,
            "content": "NAP123456ABCD",
        }
        result = await process_sepay_payment(sessions, payload)
        duplicate = await process_sepay_payment(sessions, payload)
        assert result.status == "direct_purchase_completed"
        assert len(result.order_ids) == 2
        assert result.shop_order_code is not None
        assert result.shop_order_code.startswith("B")
        assert [cipher.decrypt(value) for value in result.encrypted_secrets] == [
            "account1:password",
            "account2:password",
        ]
        assert duplicate.status == "duplicate"

        async with sessions() as session:
            user = await session.get(User, 123456)
            stock_items = list(await session.scalars(select(InventoryItem)))
            order_count = await session.scalar(select(func.count(Order.id)))
            wallet_count = int(
                await session.scalar(select(func.count(WalletTransaction.id))) or 0
            )
            assert user is not None and user.balance == 10_000
            assert all(item.status == "sold" for item in stock_items)
            assert order_count == 2
            assert wallet_count == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_direct_purchase_honors_reserved_coupon_price() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tài khoản",
                name_en="Account",
                price=50_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=0)
            session.add_all([product, user])
            await session.flush()
            coupon = DiscountCode(
                product_id=product.id,
                code="QR5K",
                discount_type="fixed",
                discount_value=5_000,
            )
            session.add(coupon)
            await session.flush()
            session.add_all(
                [
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt("account:password"),
                    ),
                    Deposit(
                        user_id=user.telegram_id,
                        code="NAP123456ABCD",
                        requested_amount=45_000,
                        payment_kind="direct_purchase",
                        product_id=product.id,
                        discount_amount=5_000,
                        discount_code_id=coupon.id,
                        discount_code=coupon.code,
                    ),
                ]
            )
            await session.commit()

        result = await process_sepay_payment(
            sessions,
            {
                "id": 22333,
                "transferType": "in",
                "transferAmount": 45_000,
                "content": "NAP123456ABCD",
            },
        )
        assert result.status == "direct_purchase_completed"

        async with sessions() as session:
            order = await session.scalar(select(Order))
            coupon = await session.scalar(select(DiscountCode))
            assert order is not None and order.amount == 45_000
            assert order.discount_amount == 5_000
            assert order.discount_code == "QR5K"
            assert coupon is not None and coupon.used_count == 1
        await engine.dispose()

    asyncio.run(scenario())


def test_direct_purchase_does_not_reuse_a_coupon_for_the_same_user() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tai khoan",
                name_en="Account",
                price=50_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=0)
            session.add_all([product, user])
            await session.flush()
            coupon = DiscountCode(
                product_id=product.id,
                code="ONCEONLY",
                discount_type="fixed",
                discount_value=5_000,
                max_uses=10,
                used_count=1,
            )
            session.add(coupon)
            await session.flush()
            sold_item = InventoryItem(
                product_id=product.id,
                encrypted_secret="sold",
                status="sold",
            )
            available_item = InventoryItem(
                product_id=product.id,
                encrypted_secret="available",
            )
            session.add_all([sold_item, available_item])
            await session.flush()
            session.add_all(
                [
                    Order(
                        user_id=user.telegram_id,
                        product_id=product.id,
                        inventory_item_id=sold_item.id,
                        amount=45_000,
                        discount_code_id=coupon.id,
                        discount_code=coupon.code,
                        status="completed",
                    ),
                    Deposit(
                        user_id=user.telegram_id,
                        code="NAP123456CDEF",
                        requested_amount=45_000,
                        payment_kind="direct_purchase",
                        product_id=product.id,
                        discount_amount=5_000,
                        discount_code_id=coupon.id,
                        discount_code=coupon.code,
                    ),
                ]
            )
            await session.commit()
            available_item_id = available_item.id

        result = await process_sepay_payment(
            sessions,
            {
                "id": 22334,
                "transferType": "in",
                "transferAmount": 45_000,
                "content": "NAP123456CDEF",
            },
        )
        assert result.status == "direct_purchase_fallback"

        async with sessions() as session:
            stored_user = await session.get(User, 123456)
            stored_coupon = await session.scalar(select(DiscountCode))
            available_item = await session.get(InventoryItem, available_item_id)
            order_count = int(await session.scalar(select(func.count(Order.id))) or 0)
            assert stored_user is not None and stored_user.balance == 45_000
            assert stored_coupon is not None and stored_coupon.used_count == 1
            assert available_item is not None and available_item.status == "available"
            assert order_count == 1
        await engine.dispose()

    asyncio.run(scenario())


def test_direct_purchase_falls_back_to_wallet_when_stock_is_gone() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tài khoản",
                name_en="Account",
                price=50_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=0)
            session.add_all([product, user])
            await session.flush()
            session.add(
                Deposit(
                    user_id=user.telegram_id,
                    code="NAP123456ABCD",
                    requested_amount=50_000,
                    payment_kind="direct_purchase",
                    product_id=product.id,
                )
            )
            await session.commit()

        result = await process_sepay_payment(
            sessions,
            {
                "id": 33333,
                "transferType": "in",
                "transferAmount": 50_000,
                "content": "NAP123456ABCD",
            },
        )
        assert result.status == "direct_purchase_fallback"

        async with sessions() as session:
            user = await session.get(User, 123456)
            wallet_transaction = await session.scalar(select(WalletTransaction))
            assert user is not None and user.balance == 50_000
            assert wallet_transaction is not None
            assert wallet_transaction.kind == "direct_purchase_fallback"
            assert wallet_transaction.amount == 50_000
        await engine.dispose()

    asyncio.run(scenario())


def test_manual_stock_zero_preserves_inventory_and_blocks_all_purchase_sources() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = FakeSupplier(balance=100_000, stock=20)
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            local_product = Product(
                category_id=category.id,
                name_vi="Kho local tạm dừng",
                name_en="Paused local",
                price=10_000,
                force_out_of_stock=True,
            )
            api_product = Product(
                category_id=category.id,
                name_vi="API tạm dừng",
                name_en="Paused API",
                price=20_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-PAUSED",
                external_stock=20,
                force_out_of_stock=True,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=100_000)
            session.add_all([local_product, api_product, user])
            await session.flush()
            item = InventoryItem(
                product_id=local_product.id,
                encrypted_secret=cipher.encrypt("preserved:account"),
            )
            session.add(item)
            await session.commit()

        async with sessions() as session:
            assert await available_stock(session, local_product.id) == 0
            assert (
                await available_stock(
                    session,
                    api_product.id,
                    supplier,  # type: ignore[arg-type]
                    refresh_external=True,
                )
                == 0
            )

        local_result = await purchase_product(
            sessions,
            user.telegram_id,
            local_product.id,
            cipher,
        )
        api_result = await purchase_product(
            sessions,
            user.telegram_id,
            api_product.id,
            cipher,
            supplier_client=supplier,  # type: ignore[arg-type]
        )

        assert local_result.message == "out_of_stock"
        assert api_result.message == "out_of_stock"
        assert supplier.buy_calls == 0
        async with sessions() as session:
            stored_item = await session.get(InventoryItem, item.id)
            stored_api_product = await session.get(Product, api_product.id)
            stored_user = await session.get(User, user.telegram_id)
            assert stored_item is not None and stored_item.status == "available"
            assert stored_api_product is not None and stored_api_product.external_stock == 20
            assert stored_user is not None and stored_user.balance == 100_000
        await engine.dispose()

    asyncio.run(scenario())


def test_direct_purchase_manual_stock_zero_falls_back_without_consuming_inventory() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tài khoản tạm dừng",
                name_en="Paused account",
                price=50_000,
                force_out_of_stock=True,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=0)
            session.add_all([product, user])
            await session.flush()
            item = InventoryItem(
                product_id=product.id,
                encrypted_secret="preserved-secret",
            )
            session.add_all(
                [
                    item,
                    Deposit(
                        user_id=user.telegram_id,
                        code="NAP123456EFGH",
                        requested_amount=50_000,
                        payment_kind="direct_purchase",
                        product_id=product.id,
                    ),
                ]
            )
            await session.commit()

        result = await process_sepay_payment(
            sessions,
            {
                "id": 33334,
                "transferType": "in",
                "transferAmount": 50_000,
                "content": "NAP123456EFGH",
            },
        )

        assert result.status == "direct_purchase_fallback"
        async with sessions() as session:
            stored_user = await session.get(User, user.telegram_id)
            stored_item = await session.get(InventoryItem, item.id)
            assert stored_user is not None and stored_user.balance == 50_000
            assert stored_item is not None and stored_item.status == "available"
        await engine.dispose()

    asyncio.run(scenario())


def test_external_purchase_uses_dynamic_price_and_delivers_accounts() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = FakeSupplier(balance=30_000, stock=100)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="ChatGPT Plus",
                name_en="ChatGPT Plus",
                price=99_000,
                allow_quantity=True,
                max_quantity=10,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_markup=5_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=50_000)
            session.add_all([product, user])
            await session.commit()

        result = await purchase_product(
            sessions,
            123456,
            product.id,
            cipher,
            2,
            supplier,  # type: ignore[arg-type]
        )
        assert result.ok is True
        assert result.total_amount == 40_000
        assert result.secrets == ["chatgpt1:password", "chatgpt2:password"]
        assert supplier.buy_calls == 1

        async with sessions() as session:
            user = await session.get(User, 123456)
            product = await session.get(Product, product.id)
            orders = list(await session.scalars(select(Order).order_by(Order.id)))
            wallet_transaction = await session.scalar(select(WalletTransaction))
            assert user is not None and user.balance == 10_000
            assert product is not None and product.price == 20_000
            assert product.external_stock == 0
            assert all(order.amount == 20_000 for order in orders)
            assert all(order.cost_amount == 15_000 for order in orders)
            assert all(order.supplier_order_code == "API-TELE-TEST123" for order in orders)
            assert len({order.batch_code for order in orders}) == 1
            assert wallet_transaction is not None
            assert wallet_transaction.kind == "product_purchase"
            assert wallet_transaction.amount == -40_000
            assert wallet_transaction.balance_before == 50_000
            assert wallet_transaction.balance_after == 10_000
            assert wallet_transaction.reference_id == orders[0].batch_code
        await engine.dispose()

    asyncio.run(scenario())


def test_external_purchase_recovers_supplier_order_after_timeout() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = TimeoutRecoveringSupplier(balance=30_000, stock=100)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="ChatGPT Plus",
                name_en="ChatGPT Plus",
                price=20_000,
                allow_quantity=True,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_markup=5_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=50_000)
            session.add_all([product, user])
            await session.commit()

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            2,
            supplier,  # type: ignore[arg-type]
        )

        assert result.ok is True
        assert result.secrets == ["recovered1:password", "recovered2:password"]
        async with sessions() as session:
            orders = list(await session.scalars(select(Order).order_by(Order.id)))
            assert len(orders) == 2
            assert all(
                order.supplier_order_code == "API-TELE-RECOVERED" for order in orders
            )
        await engine.dispose()

    asyncio.run(scenario())


def test_external_purchase_queues_late_recovery_without_charging_user() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = PendingRecoverySupplier(balance=30_000, stock=100)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="ChatGPT Plus",
                name_en="ChatGPT Plus",
                price=20_000,
                allow_quantity=True,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_markup=5_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=50_000)
            session.add_all([product, user])
            await session.commit()

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            2,
            supplier,  # type: ignore[arg-type]
            supplier_idempotency_key="shop-api-pending-recovery",
        )

        repeated = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            2,
            supplier,  # type: ignore[arg-type]
            supplier_idempotency_key="shop-api-pending-recovery",
        )

        assert result.ok is False
        assert result.message == "supplier_unavailable"
        assert repeated.ok is False
        assert repeated.message == "supplier_unavailable"
        assert supplier.buy_calls == 1
        async with sessions() as session:
            stored_user = await session.get(User, user.telegram_id)
            recovery = await session.scalar(select(SupplierRecoveryRequest))
            assert stored_user is not None and stored_user.balance == 50_000
            assert recovery is not None and recovery.status == "pending"
            assert recovery.product_id == product.id
            assert recovery.supplier_product_id == "SP-GEF55PBV"
            assert recovery.quantity == 2
        await engine.dispose()

    asyncio.run(scenario())


def test_recovered_supplier_inventory_is_sold_before_buying_again() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = FakeSupplier(balance=0, stock=0)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="ChatGPT Plus",
                name_en="ChatGPT Plus",
                price=20_000,
                allow_quantity=True,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_markup=5_000,
                external_stock=2,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=50_000)
            session.add_all([product, user])
            await session.flush()
            session.add_all(
                [
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt(f"orphan{index}:password"),
                        cost_amount=15_000,
                        supplier_order_code="API-TELE-ORPHAN",
                        supplier_item_index=index,
                    )
                    for index in range(2)
                ]
            )
            await session.commit()

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            2,
            supplier,  # type: ignore[arg-type]
        )

        assert result.ok is True
        assert supplier.buy_calls == 0
        assert result.secrets == ["orphan0:password", "orphan1:password"]
        async with sessions() as session:
            orders = list(await session.scalars(select(Order).order_by(Order.id)))
            assert all(order.cost_amount == 15_000 for order in orders)
            assert all(order.supplier_order_code == "API-TELE-ORPHAN" for order in orders)
        await engine.dispose()

    asyncio.run(scenario())


def test_last_locked_inventory_item_releases_dynamic_price() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = FakeSupplier(balance=100_000, stock=100)
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Hàng ôm",
                name_en="Stocked item",
                price=28_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_price=27_000,
                supplier_markup=8_000,
                price_lock_enabled=True,
                external_stock=1,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=28_000)
            session.add_all([product, user])
            await session.flush()
            session.add(
                InventoryItem(
                    product_id=product.id,
                    encrypted_secret=cipher.encrypt("stocked:password"),
                    cost_amount=20_000,
                )
            )
            await session.commit()

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            1,
            supplier,  # type: ignore[arg-type]
        )

        assert result.ok is True
        assert result.total_amount == 28_000
        assert supplier.buy_calls == 0
        async with sessions() as session:
            stored_product = await session.get(Product, product.id)
            assert stored_product is not None
            assert stored_product.price_lock_enabled is False
            assert stored_product.price == 35_000
            assert stored_product.external_stock == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_locked_inventory_can_fill_missing_quantity_from_api() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = FakeSupplier(balance=1_000_000, stock=100)
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Hàng ôm",
                name_en="Stocked item",
                price=28_000,
                allow_quantity=True,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_price=20_000,
                supplier_markup=8_000,
                price_lock_enabled=True,
                external_stock=1,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=100_000)
            session.add_all([product, user])
            await session.flush()
            session.add(
                InventoryItem(
                    product_id=product.id,
                    encrypted_secret=cipher.encrypt("stocked:password"),
                    cost_amount=20_000,
                )
            )
            await session.commit()

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            2,
            supplier,  # type: ignore[arg-type]
        )

        assert result.ok is True
        assert result.total_amount == 56_000
        assert supplier.buy_calls == 1
        async with sessions() as session:
            stored_product = await session.get(Product, product.id)
            available_items = int(
                await session.scalar(
                    select(func.count(InventoryItem.id)).where(
                        InventoryItem.product_id == product.id,
                        InventoryItem.status == "available",
                    )
                )
                or 0
            )
            assert stored_product is not None and stored_product.price_lock_enabled is True
            assert stored_product.external_stock == 65
            assert available_items == 1
        await engine.dispose()

    asyncio.run(scenario())


def test_external_stock_is_zero_when_supplier_balance_is_insufficient() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = FakeSupplier(balance=0, stock=100)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="ChatGPT Plus",
                name_en="ChatGPT Plus",
                price=20_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_markup=5_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=100_000)
            session.add_all([product, user])
            await session.commit()

        result = await purchase_product(
            sessions,
            123456,
            product.id,
            cipher,
            1,
            supplier,  # type: ignore[arg-type]
        )
        assert result.ok is False
        assert result.message == "out_of_stock"
        assert supplier.buy_calls == 0
        async with sessions() as session:
            product = await session.get(Product, product.id)
            assert product is not None and product.external_stock == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_external_direct_payment_delivers_supplier_account() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = FakeSupplier(balance=100_000, stock=100)
        fulfillment_events: list[tuple[int, str]] = []
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="ChatGPT Plus",
                name_en="ChatGPT Plus",
                price=20_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_markup=5_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=0)
            session.add_all([product, user])
            await session.flush()
            session.add(
                Deposit(
                    user_id=user.telegram_id,
                    code="NAP123456ABCD",
                    requested_amount=20_000,
                    payment_kind="direct_purchase",
                    product_id=product.id,
                )
            )
            await session.commit()

        result = await process_sepay_payment(
            sessions,
            {
                "id": 44444,
                "transferType": "in",
                "transferAmount": 20_000,
                "content": "NAP123456ABCD",
            },
            cipher=cipher,
            supplier_client=supplier,  # type: ignore[arg-type]
            on_fulfillment_started=lambda user_id, language: _record_fulfillment_event(
                fulfillment_events,
                user_id,
                language,
            ),
        )
        assert result.status == "direct_purchase_completed"
        assert fulfillment_events == [(123456, "vi")]
        assert [cipher.decrypt(value) for value in result.encrypted_secrets] == [
            "chatgpt1:password"
        ]
        async with sessions() as session:
            order = await session.scalar(select(Order))
            assert order is not None and order.amount == 20_000
            assert order.cost_amount == 15_000
            assert order.supplier_order_code == "API-TELE-TEST123"
        await engine.dispose()

    asyncio.run(scenario())


async def _record_fulfillment_event(
    events: list[tuple[int, str]],
    user_id: int,
    language: str,
) -> None:
    events.append((user_id, language))


def test_external_direct_payment_uses_recovered_inventory_first() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = FakeSupplier(balance=0, stock=0)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="ChatGPT Plus",
                name_en="ChatGPT Plus",
                price=20_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_markup=5_000,
                external_stock=1,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=0)
            session.add_all([product, user])
            await session.flush()
            session.add(
                InventoryItem(
                    product_id=product.id,
                    encrypted_secret=cipher.encrypt("recovered|password"),
                    cost_amount=12_000,
                    supplier_order_code="API-TELE-ORPHAN",
                    supplier_item_index=0,
                )
            )
            session.add(
                Deposit(
                    user_id=user.telegram_id,
                    code="NAP123456ABCD",
                    requested_amount=20_000,
                    payment_kind="direct_purchase",
                    product_id=product.id,
                )
            )
            await session.commit()

        result = await process_sepay_payment(
            sessions,
            {
                "id": 44444,
                "transferType": "in",
                "transferAmount": 20_000,
                "content": "NAP123456ABCD",
            },
            cipher=cipher,
            supplier_client=supplier,  # type: ignore[arg-type]
        )

        assert result.status == "direct_purchase_completed"
        assert supplier.buy_calls == 0
        assert [cipher.decrypt(value) for value in result.encrypted_secrets] == [
            "recovered|password"
        ]
        async with sessions() as session:
            order = await session.scalar(select(Order))
            assert order is not None and order.cost_amount == 12_000
            assert order.supplier_order_code == "API-TELE-ORPHAN"
        await engine.dispose()

    asyncio.run(scenario())


def test_locked_inventory_qr_never_falls_through_to_supplier_api() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = FakeSupplier(balance=1_000_000, stock=100)
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Hàng ôm",
                name_en="Stocked item",
                price=28_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_price=20_000,
                supplier_markup=8_000,
                price_lock_enabled=True,
                external_stock=1,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=0)
            session.add_all([product, user])
            await session.flush()
            item = InventoryItem(
                product_id=product.id,
                encrypted_secret=cipher.encrypt("stocked:password"),
                cost_amount=20_000,
            )
            session.add(item)
            await session.commit()

            deposit = await create_deposit(
                session,
                user.telegram_id,
                28_000,
                payment_kind="direct_purchase",
                product_id=product.id,
            )
            assert deposit.inventory_price_locked is True

            item.status = "sold"
            product.price_lock_enabled = False
            product.external_stock = 0
            await session.commit()

        result = await process_sepay_payment(
            sessions,
            {
                "id": 55555,
                "transferType": "in",
                "transferAmount": 28_000,
                "content": deposit.code,
            },
            cipher=cipher,
            supplier_client=supplier,  # type: ignore[arg-type]
        )

        assert result.status == "direct_purchase_fallback"
        assert supplier.buy_calls == 0
        async with sessions() as session:
            stored_user = await session.get(User, user.telegram_id)
            assert stored_user is not None and stored_user.balance == 28_000
        await engine.dispose()

    asyncio.run(scenario())


def test_locked_inventory_qr_can_use_supplier_stock() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = FakeSupplier(balance=1_000_000, stock=100)
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Stocked item",
                name_en="Stocked item",
                price=28_000,
                allow_quantity=True,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_price=20_000,
                supplier_markup=8_000,
                price_lock_enabled=True,
                external_stock=1,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=0)
            session.add_all([product, user])
            await session.flush()
            session.add(
                InventoryItem(
                    product_id=product.id,
                    encrypted_secret=cipher.encrypt("stocked:password"),
                    cost_amount=20_000,
                )
            )
            await session.commit()

            deposit = await create_deposit(
                session,
                user.telegram_id,
                56_000,
                payment_kind="direct_purchase",
                product_id=product.id,
                quantity=2,
            )
            assert deposit.inventory_price_locked is True

        result = await process_sepay_payment(
            sessions,
            {
                "id": 55556,
                "transferType": "in",
                "transferAmount": 56_000,
                "content": deposit.code,
            },
            cipher=cipher,
            supplier_client=supplier,  # type: ignore[arg-type]
        )

        assert result.status == "direct_purchase_completed"
        assert result.quantity == 2
        assert supplier.buy_calls == 1
        async with sessions() as session:
            stored_product = await session.get(Product, product.id)
            available_items = int(
                await session.scalar(
                    select(func.count(InventoryItem.id)).where(
                        InventoryItem.product_id == product.id,
                        InventoryItem.status == "available",
                    )
                )
                or 0
            )
            assert stored_product is not None
            assert stored_product.price_lock_enabled is True
            assert stored_product.external_stock == 65
            assert available_items == 1
        await engine.dispose()

    asyncio.run(scenario())


def test_direct_qr_purchase_pays_referral_commission() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            referrer = User(telegram_id=70001, full_name="Referrer", balance=0)
            buyer = User(
                telegram_id=70002,
                full_name="Buyer",
                balance=0,
                referred_by_id=referrer.telegram_id,
            )
            category = Category(name_vi="Tài khoản", name_en="Accounts")
            session.add_all([referrer, buyer, category])
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tài khoản QR",
                name_en="QR account",
                price=20_000,
            )
            session.add(product)
            await session.flush()
            session.add(
                InventoryItem(
                    product_id=product.id,
                    encrypted_secret=cipher.encrypt("qr-account|password"),
                )
            )
            session.add(
                Deposit(
                    user_id=buyer.telegram_id,
                    code="NAP70002ABCD",
                    requested_amount=20_000,
                    payment_kind="direct_purchase",
                    product_id=product.id,
                )
            )
            await session.commit()

        result = await process_sepay_payment(
            sessions,
            {
                "id": 70002001,
                "transferType": "in",
                "transferAmount": 20_000,
                "content": "NAP70002ABCD",
            },
            cipher=cipher,
            referral_commission_percent=5,
        )
        assert result.status == "direct_purchase_completed"
        async with sessions() as session:
            referrer = await session.get(User, 70001)
            reward = await session.scalar(select(ReferralReward))
            assert referrer is not None and referrer.balance == 1_000
            assert reward is not None and reward.order_amount == 20_000
            assert reward.commission_amount == 1_000
            assert reward.sales_channel == "telegram"
        await engine.dispose()

    asyncio.run(scenario())
