import asyncio
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import (
    Category,
    Deposit,
    DiscountCode,
    InventoryItem,
    Order,
    Product,
    User,
)
from app.services import (
    order_bundle,
    process_sepay_payment,
    purchase_product,
    recent_orders,
    user_activity_stats,
)
from app.suppliers import SupplierPurchase, SupplierSnapshot
from app.utils import SecretCipher


async def make_database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


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
        assert another_transfer.status == "credited"

        async with sessions() as session:
            user = await session.get(User, 123456)
            assert user is not None
            assert user.balance == 200_000
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
            assert user is not None and user.balance == 10_000
            assert all(item.status == "sold" for item in stock_items)
            assert order_count == 2
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
            assert user is not None and user.balance == 50_000
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
            assert user is not None and user.balance == 10_000
            assert product is not None and product.price == 20_000
            assert product.external_stock == 0
            assert all(order.amount == 20_000 for order in orders)
            assert all(order.cost_amount == 15_000 for order in orders)
            assert all(order.supplier_order_code == "API-TELE-TEST123" for order in orders)
            assert len({order.batch_code for order in orders}) == 1
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
        )
        assert result.status == "direct_purchase_completed"
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
