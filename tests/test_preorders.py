import asyncio

from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Category, InventoryItem, Order, Preorder, Product, User
from app.preorders import (
    PreorderError,
    _claim_next_preorder,
    _process_claimed_preorder,
    create_preorder,
    preorder_unit_price,
    preorderable_products,
)
from app.services import purchase_product
from app.suppliers import SupplierError, SupplierPurchase, SupplierSnapshot
from app.utils import SecretCipher


async def make_database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def seed_product(
    sessions,
    *,
    price: int = 10_000,
    balance: int = 100_000,
) -> tuple[int, int]:
    async with sessions() as session:
        category = Category(name_vi="ChatGPT", name_en="ChatGPT", active=True)
        session.add(category)
        await session.flush()
        product = Product(
            category_id=category.id,
            name_vi="GPT Plus",
            name_en="GPT Plus",
            price=price,
            allow_quantity=True,
            max_quantity=10,
            fulfillment_source="local",
            active=True,
        )
        user = User(
            telegram_id=1001,
            full_name="Customer",
            username="customer",
            balance=balance,
        )
        session.add_all([product, user])
        await session.commit()
        return product.id, user.telegram_id


def test_preorder_price_adds_five_percent() -> None:
    assert preorder_unit_price(10_000) == 10_500
    assert preorder_unit_price(10_001) == 10_502


def test_preorder_creation_checks_wallet_without_deducting() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        product_id, user_id = await seed_product(sessions)

        async with sessions() as session:
            preorder = await create_preorder(
                session,
                user_id,
                product_id,
                2,
                expected_base_unit_price=10_000,
                max_active_per_user=5,
            )
            await session.commit()
            assert preorder.total_amount == 21_000

        async with sessions() as session:
            user = await session.get(User, user_id)
            assert user is not None
            assert user.balance == 100_000
            assert await session.scalar(select(func.count(Preorder.id))) == 1
        await engine.dispose()

    asyncio.run(scenario())


def test_preorder_rejects_duplicate_active_product() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        product_id, user_id = await seed_product(sessions)
        async with sessions() as session:
            await create_preorder(
                session,
                user_id,
                product_id,
                1,
                expected_base_unit_price=10_000,
                max_active_per_user=5,
            )
            await session.commit()
        async with sessions() as session:
            try:
                await create_preorder(
                    session,
                    user_id,
                    product_id,
                    1,
                    expected_base_unit_price=10_000,
                    max_active_per_user=5,
                )
            except PreorderError as exc:
                assert exc.code == "duplicate"
            else:
                raise AssertionError("duplicate preorder was accepted")
        await engine.dispose()

    asyncio.run(scenario())


def test_only_live_out_of_stock_account_products_are_preorderable() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="Shop", name_en="Shop", active=True)
            session.add(category)
            await session.flush()
            products = [
                Product(
                    category_id=category.id,
                    name_vi="Cho đặt",
                    name_en="Open",
                    price=10_000,
                    fulfillment_source="local",
                ),
                Product(
                    category_id=category.id,
                    name_vi="Có hàng",
                    name_en="In stock",
                    price=10_000,
                    fulfillment_source="local",
                ),
                Product(
                    category_id=category.id,
                    name_vi="Khóa 0",
                    name_en="Forced zero",
                    price=10_000,
                    fulfillment_source="local",
                    force_out_of_stock=True,
                ),
                Product(
                    category_id=category.id,
                    name_vi="Thuê SMS",
                    name_en="SMS",
                    price=2_000,
                    product_type="sms",
                    fulfillment_source="local",
                ),
                Product(
                    category_id=category.id,
                    name_vi="Đang ẩn",
                    name_en="Hidden",
                    price=10_000,
                    fulfillment_source="local",
                    active=False,
                ),
            ]
            session.add_all(products)
            await session.flush()
            session.add(
                InventoryItem(
                    product_id=products[1].id,
                    encrypted_secret="stock",
                    status="available",
                )
            )
            await session.commit()

        async with sessions() as session:
            available = await preorderable_products(session)
            assert [product.name_vi for product in available] == ["Cho đặt"]
        await engine.dispose()

    asyncio.run(scenario())


def test_local_preorder_fulfills_once_and_deducts_exact_fixed_price() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        product_id, user_id = await seed_product(sessions)
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            preorder = await create_preorder(
                session,
                user_id,
                product_id,
                2,
                expected_base_unit_price=10_000,
                max_active_per_user=5,
            )
            await session.commit()
            preorder_id = preorder.id
        async with sessions() as session:
            session.add_all(
                [
                    InventoryItem(
                        product_id=product_id,
                        encrypted_secret=cipher.encrypt("account-a"),
                        cost_amount=7_000,
                        status="available",
                    ),
                    InventoryItem(
                        product_id=product_id,
                        encrypted_secret=cipher.encrypt("account-b"),
                        cost_amount=7_000,
                        status="available",
                    ),
                ]
            )
            await session.commit()

        claimed = await _claim_next_preorder(sessions)
        assert claimed is not None
        await _process_claimed_preorder(
            sessions,
            claimed,
            cipher,
            None,
            None,
            None,
            None,
            None,
            0,
        )

        async with sessions() as session:
            preorder = await session.get(Preorder, preorder_id)
            user = await session.get(User, user_id)
            orders = list(
                await session.scalars(
                    select(Order).where(Order.preorder_id == preorder_id).order_by(Order.id)
                )
            )
            assert preorder is not None and preorder.status == "completed"
            assert user is not None and user.balance == 79_000
            assert [order.amount for order in orders] == [10_500, 10_500]
            assert all(order.sales_channel == "preorder" for order in orders)

        repeated = await purchase_product(
            sessions,
            user_id,
            product_id,
            cipher,
            2,
            preorder_id=preorder_id,
            expected_base_unit_price=10_000,
            fixed_unit_price=10_500,
        )
        assert repeated.ok is True
        assert repeated.secrets == ["account-a", "account-b"]
        async with sessions() as session:
            user = await session.get(User, user_id)
            assert user is not None and user.balance == 79_000
            assert (
                await session.scalar(
                    select(func.count(Order.id)).where(Order.preorder_id == preorder_id)
                )
                == 2
            )
        await engine.dispose()

    asyncio.run(scenario())


def test_price_change_cancels_without_charging() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        product_id, user_id = await seed_product(sessions)
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            preorder = await create_preorder(
                session,
                user_id,
                product_id,
                1,
                expected_base_unit_price=10_000,
                max_active_per_user=5,
            )
            await session.commit()
            preorder_id = preorder.id
        async with sessions() as session:
            product = await session.get(Product, product_id)
            assert product is not None
            product.price = 12_000
            session.add(
                InventoryItem(
                    product_id=product_id,
                    encrypted_secret=cipher.encrypt("account"),
                    status="available",
                )
            )
            await session.commit()

        claimed = await _claim_next_preorder(sessions)
        assert claimed is not None
        await _process_claimed_preorder(
            sessions, claimed, cipher, None, None, None, None, None, 0
        )
        async with sessions() as session:
            preorder = await session.get(Preorder, preorder_id)
            user = await session.get(User, user_id)
            assert preorder is not None
            assert preorder.status == "cancelled"
            assert preorder.cancel_reason == "price_changed"
            assert user is not None and user.balance == 100_000
            assert await session.scalar(select(func.count(Order.id))) == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_insufficient_wallet_at_fulfillment_cancels_without_delivery() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        product_id, user_id = await seed_product(sessions, balance=20_000)
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            preorder = await create_preorder(
                session,
                user_id,
                product_id,
                1,
                expected_base_unit_price=10_000,
                max_active_per_user=5,
            )
            await session.commit()
            preorder_id = preorder.id
        async with sessions() as session:
            user = await session.get(User, user_id)
            assert user is not None
            user.balance = 5_000
            session.add(
                InventoryItem(
                    product_id=product_id,
                    encrypted_secret=cipher.encrypt("account"),
                    status="available",
                )
            )
            await session.commit()

        claimed = await _claim_next_preorder(sessions)
        assert claimed is not None
        await _process_claimed_preorder(
            sessions, claimed, cipher, None, None, None, None, None, 0
        )
        async with sessions() as session:
            preorder = await session.get(Preorder, preorder_id)
            user = await session.get(User, user_id)
            assert preorder is not None
            assert preorder.status == "cancelled"
            assert preorder.cancel_reason == "insufficient_balance"
            assert user is not None and user.balance == 5_000
            assert await session.scalar(select(func.count(Order.id))) == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_preorders_are_fulfilled_fifo_per_product() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        product_id, first_user_id = await seed_product(sessions)
        second_user_id = 1002
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            session.add(User(telegram_id=second_user_id, full_name="Second", balance=100_000))
            await session.commit()
        async with sessions() as session:
            first = await create_preorder(
                session,
                first_user_id,
                product_id,
                1,
                expected_base_unit_price=10_000,
                max_active_per_user=5,
            )
            await session.commit()
            first_id = first.id
        async with sessions() as session:
            second = await create_preorder(
                session,
                second_user_id,
                product_id,
                1,
                expected_base_unit_price=10_000,
                max_active_per_user=5,
            )
            await session.commit()
            second_id = second.id
        async with sessions() as session:
            session.add(
                InventoryItem(
                    product_id=product_id,
                    encrypted_secret=cipher.encrypt("first-account"),
                    status="available",
                )
            )
            await session.commit()

        claimed = await _claim_next_preorder(sessions)
        assert claimed is not None and claimed.id == first_id
        await _process_claimed_preorder(
            sessions, claimed, cipher, None, None, None, None, None, 0
        )
        async with sessions() as session:
            first = await session.get(Preorder, first_id)
            second = await session.get(Preorder, second_id)
            assert first is not None and first.status == "completed"
            assert second is not None and second.status == "pending"
            assert (
                await session.scalar(
                    select(func.count(Order.id)).where(Order.user_id == second_user_id)
                )
                == 0
            )
        await engine.dispose()

    asyncio.run(scenario())


class PreorderSupplier:
    provider = "sumistore"

    def __init__(self, *, stock: int, fail: bool = False) -> None:
        self.stock = stock
        self.fail = fail
        self.balance = 100_000
        self.buy_calls = 0

    async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
        return SupplierSnapshot(
            product_id=product_id,
            name="GPT API",
            description="",
            unit_price=15_000,
            source_stock=self.stock,
            owner_balance=self.balance,
        )

    async def buy(self, product_id: str, quantity: int) -> SupplierPurchase:
        self.buy_calls += 1
        if self.fail:
            raise SupplierError("SUPPLIER_UNAVAILABLE")
        self.stock -= quantity
        self.balance -= 15_000 * quantity
        return SupplierPurchase(
            order_code=f"SUMI-{self.buy_calls}",
            unit_price=15_000,
            accounts=tuple(f"api-account-{index}" for index in range(quantity)),
            product_id=product_id,
            provider=self.provider,
        )


def test_api_preorder_uses_supplier_and_keeps_provider_private_from_order_flow() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = PreorderSupplier(stock=0)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus API",
                name_en="GPT Plus API",
                price=20_000,
                allow_quantity=True,
                max_quantity=5,
                fulfillment_source="sumistore",
                supplier_product_id="SP-PREORDER",
                supplier_price=15_000,
                supplier_markup=5_000,
                external_stock=0,
            )
            user = User(telegram_id=2001, full_name="API buyer", balance=100_000)
            session.add_all([product, user])
            await session.commit()
            product_id = product.id
            user_id = user.telegram_id
        async with sessions() as session:
            preorder = await create_preorder(
                session,
                user_id,
                product_id,
                1,
                expected_base_unit_price=20_000,
                max_active_per_user=5,
            )
            await session.commit()
            preorder_id = preorder.id

        supplier.stock = 1
        async with sessions() as session:
            product = await session.get(Product, product_id)
            assert product is not None
            product.external_stock = 1
            await session.commit()
        claimed = await _claim_next_preorder(sessions)
        assert claimed is not None
        await _process_claimed_preorder(
            sessions,
            claimed,
            cipher,
            supplier,  # type: ignore[arg-type]
            None,
            None,
            None,
            None,
            0,
        )

        async with sessions() as session:
            preorder = await session.get(Preorder, preorder_id)
            user = await session.get(User, user_id)
            order = await session.scalar(select(Order).where(Order.preorder_id == preorder_id))
            assert preorder is not None and preorder.status == "completed"
            assert user is not None and user.balance == 79_000
            assert order is not None and order.amount == 21_000
            assert supplier.buy_calls == 1
        await engine.dispose()

    asyncio.run(scenario())


def test_transient_supplier_failure_stays_pending_and_does_not_charge() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = PreorderSupplier(stock=1, fail=True)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT lỗi tạm",
                name_en="Temporary API failure",
                price=20_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-RETRY",
                supplier_price=15_000,
                supplier_markup=5_000,
                external_stock=0,
            )
            user = User(telegram_id=3001, full_name="Retry buyer", balance=100_000)
            session.add_all([product, user])
            await session.commit()
            product_id = product.id
            user_id = user.telegram_id
        async with sessions() as session:
            preorder = await create_preorder(
                session,
                user_id,
                product_id,
                1,
                expected_base_unit_price=20_000,
                max_active_per_user=5,
            )
            await session.commit()
            preorder_id = preorder.id
        async with sessions() as session:
            product = await session.get(Product, product_id)
            assert product is not None
            product.external_stock = 1
            await session.commit()

        claimed = await _claim_next_preorder(sessions)
        assert claimed is not None
        await _process_claimed_preorder(
            sessions,
            claimed,
            cipher,
            supplier,  # type: ignore[arg-type]
            None,
            None,
            None,
            None,
            0,
        )
        async with sessions() as session:
            preorder = await session.get(Preorder, preorder_id)
            user = await session.get(User, user_id)
            assert preorder is not None and preorder.status == "pending"
            assert preorder.last_error == "supplier_unavailable"
            assert user is not None and user.balance == 100_000
            assert await session.scalar(select(func.count(Order.id))) == 0
        await engine.dispose()

    asyncio.run(scenario())
