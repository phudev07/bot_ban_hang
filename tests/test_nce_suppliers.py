import asyncio

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.keyboards import main_menu, nce_family_menu, products_menu
from app.models import Category, InventoryItem, Order, Product, SupplierBalanceTransaction, User
from app.nce_suppliers import (
    ensure_nce_local_products,
    nce_product_family,
    nce_token_millions,
)
from app.services import purchase_product
from app.utils import SecretCipher


def test_nce_product_names_are_grouped_without_a_supplier_source() -> None:
    assert nce_product_family("API CODEX - 50M token") == "codex"
    assert nce_product_family("Claude API - 100 million token") == "claude"
    assert nce_product_family("GPT Plus") is None
    assert nce_token_millions("API CODEX - 50M token") == 50
    assert nce_token_millions("Claude 100 triệu token") == 100
    assert nce_token_millions("Codex 20M token") is None


def test_nce_products_migrate_to_local_cdk_inventory_and_purchase_locally() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="API CODEX & CLAUDE", name_en="API", position=3)
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="API CODEX - 50M token",
                name_en="CODEX API - 50M tokens",
                price=40_000,
                fulfillment_source="nce",
                supplier_product_id="3",
                supplier_price=35_000,
                supplier_markup=5_000,
                external_stock=10,
                max_quantity=1,
            )
            user = User(telegram_id=123, full_name="Buyer", balance=100_000)
            session.add_all([product, user])
            await session.commit()
            product_id = product.id
            category_id = category.id

        await ensure_nce_local_products(sessions)
        async with sessions() as session:
            products = list(
                await session.scalars(
                    select(Product)
                    .where(Product.category_id == category_id)
                    .order_by(Product.id)
                )
            )
            migrated = await session.get(Product, product_id)
            assert len(products) == 1
            assert migrated is not None
            assert migrated.fulfillment_source == "local"
            assert migrated.supplier_product_id is None
            assert migrated.supplier_price is None
            assert migrated.supplier_markup == 0
            assert migrated.external_stock == 0
            assert migrated.price == 40_000
            session.add(
                InventoryItem(
                    product_id=product_id,
                    encrypted_secret=cipher.encrypt("cdk-codex-local-001"),
                    cost_amount=35_000,
                )
            )
            await session.commit()

        result = await purchase_product(sessions, 123, product_id, cipher)

        assert result.ok
        assert result.secrets == ["cdk-codex-local-001"]
        async with sessions() as session:
            order = await session.scalar(select(Order))
            audit = await session.scalar(
                select(SupplierBalanceTransaction).where(
                    SupplierBalanceTransaction.provider == "nce"
                )
            )
            item = await session.scalar(select(InventoryItem))
            user = await session.get(User, 123)
            assert order is not None
            assert order.supplier_provider is None
            assert order.cost_amount == 35_000
            assert audit is None
            assert item is not None and item.status == "sold"
            assert user is not None and user.balance == 60_000
        await engine.dispose()

    asyncio.run(scenario())


def test_nce_local_catalog_is_seeded_once_for_an_empty_shop() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        await ensure_nce_local_products(sessions)
        await ensure_nce_local_products(sessions)
        async with sessions() as session:
            products = list(await session.scalars(select(Product).order_by(Product.name_vi)))
            assert len(products) == 5
            assert all(product.fulfillment_source == "local" for product in products)
            assert all(product.supplier_product_id is None for product in products)
            assert {product.price for product in products} == {40_000, 80_000, 220_000}
        await engine.dispose()

    asyncio.run(scenario())


def test_nce_navigation_keeps_quick_buy_flat_and_family_buttons() -> None:
    main = main_menu("vi", sms_enabled=True, nce_enabled=True)
    rows = [[button.callback_data for button in row] for row in main.inline_keyboard]
    assert ["menu:sms"] in rows
    assert rows.index(["menu:nce"]) == rows.index(["menu:sms"]) + 1

    products = [
        Product(
            id=1,
            category_id=3,
            name_vi="API CODEX - 50M token",
            name_en="CODEX API - 50M tokens",
            price=40_000,
            fulfillment_source="local",
        ),
        Product(
            id=2,
            category_id=3,
            name_vi="API CLAUDE - 50M token",
            name_en="CLAUDE API - 50M tokens",
            price=40_000,
            fulfillment_source="local",
        ),
    ]
    quick = products_menu(products, "vi", "back:menu")
    quick_callbacks = [
        button.callback_data for row in quick.inline_keyboard for button in row
    ]
    assert quick_callbacks == ["prod:1", "prod:2", "back:menu"]

    family = nce_family_menu(3, "vi", products)
    family_callbacks = [
        button.callback_data for row in family.inline_keyboard for button in row
    ]
    assert "nce-family:3:codex" in family_callbacks
    assert "nce-family:3:claude" in family_callbacks
