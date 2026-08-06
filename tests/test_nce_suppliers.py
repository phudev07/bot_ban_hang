import asyncio
import json

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.keyboards import main_menu, nce_family_menu, products_menu
from app.models import Category, Order, Product, SupplierBalanceTransaction, User
from app.nce_suppliers import NceClient, ensure_nce_products
from app.services import purchase_product
from app.suppliers import SupplierPurchase, SupplierSnapshot
from app.utils import SecretCipher


def catalog_payload() -> dict[str, object]:
    return {
        "success": True,
        "products": [
            {
                "_id": "3",
                "product_name": {"plain": "API CODEX | 50M Token | 1D | BHF"},
                "description": {"plain": "Full warranty"},
                "effectivePricing": 35_000,
                "stock": 94,
                "status": "active",
                "is_active": True,
            },
            {
                "_id": "4",
                "product_name": {"plain": "API CODEX | 100M Token | 1D | BHF"},
                "effectivePricing": 70_000,
                "stock": 99,
                "status": "active",
                "is_active": True,
            },
            {
                "_id": "5",
                "product_name": {"plain": "API CODEX | 500M Token | 3D | BHF"},
                "effectivePricing": 200_000,
                "stock": 99,
                "status": "active",
                "is_active": True,
            },
            {
                "_id": "1",
                "product_name": {"plain": "API Claude | 50M Token | 1D | BHF"},
                "effectivePricing": 35_000,
                "stock": 84,
                "status": "active",
                "is_active": True,
            },
            {
                "_id": "trial",
                "product_name": {"plain": "API trial | 2M Token | 1D"},
                "effectivePricing": 2_000,
                "stock": 100,
                "status": "active",
                "is_active": True,
            },
        ],
    }


def test_nce_catalog_uses_bearer_auth_and_supported_markups() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer sk-test-only"
        if request.url.path.endswith("/products"):
            return httpx.Response(200, json=catalog_payload())
        if request.url.path.endswith("/balance"):
            return httpx.Response(200, json={"success": True, "balanceVnd": 350_000})
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        client = NceClient(
            "https://api.example.test",
            "sk-test-only",
            transport=httpx.MockTransport(handler),
        )
        products = await client.refresh_catalog(force=True)
        snapshot = await client.fetch_snapshot("3")

        assert [(item.family, item.token_millions, item.markup) for item in products] == [
            ("codex", 50, 5_000),
            ("codex", 100, 10_000),
            ("codex", 500, 20_000),
            ("claude", 50, 5_000),
        ]
        assert snapshot.unit_price == 35_000
        assert snapshot.source_stock == 94
        assert snapshot.effective_stock == 10
        assert len(requests) == 2
        await client.aclose()

    asyncio.run(scenario())


def test_nce_purchase_recovers_timeout_without_second_post() -> None:
    post_count = 0
    order_list_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count, order_list_count
        if request.method == "GET" and request.url.path.endswith("/orders"):
            order_list_count += 1
            orders = [] if order_list_count == 1 else [
                {
                    "order_code": "ORDER-1",
                    "product_id": 3,
                    "quantity": 1,
                    "status": "completed",
                }
            ]
            return httpx.Response(200, json={"success": True, "orders": orders})
        if request.method == "GET" and request.url.path.endswith("/orders/ORDER-1"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "order": {
                        "order_code": "ORDER-1",
                        "product_id": 3,
                        "quantity": 1,
                        "total_amount": 35_000,
                        "status": "completed",
                        "delivery_content": "ACTIVATION-CODE-ONE",
                    },
                },
            )
        if request.method == "POST":
            post_count += 1
            assert request.headers["Idempotency-Key"] == "shop-order-1"
            assert json.loads((await request.aread()).decode())["product_id"] == "3"
            raise httpx.ReadTimeout("ambiguous timeout", request=request)
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        client = NceClient(
            "https://api.example.test",
            "sk-test-only",
            transport=httpx.MockTransport(handler),
        )
        purchase = await client.buy("3", 1, idempotency_key="shop-order-1")

        assert post_count == 1
        assert purchase.order_code == "NCE-ORDER-1"
        assert purchase.unit_price == 35_000
        assert purchase.accounts == ("ACTIVATION-CODE-ONE",)
        await client.aclose()

    asyncio.run(scenario())


def test_nce_products_are_created_with_dynamic_prices() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/products"):
            return httpx.Response(200, json=catalog_payload())
        if request.url.path.endswith("/balance"):
            return httpx.Response(200, json={"success": True, "balanceVnd": 350_000})
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        client = NceClient(
            "https://api.example.test",
            "sk-test-only",
            transport=httpx.MockTransport(handler),
        )

        await ensure_nce_products(sessions, client)
        async with sessions() as session:
            category = await session.scalar(
                select(Category).where(Category.name_vi == "API CODEX & CLAUDE")
            )
            products = list(
                await session.scalars(
                    select(Product)
                    .where(Product.fulfillment_source == "nce")
                    .order_by(Product.supplier_product_id)
                )
            )

        assert category is not None and category.position == 3
        assert len(products) == 4
        assert {product.price for product in products} == {40_000, 80_000, 220_000}
        assert all(product.max_quantity == 1 for product in products)
        assert all(not product.allow_quantity for product in products)
        await client.aclose()
        await engine.dispose()

    asyncio.run(scenario())


class WalletNceSupplier:
    provider = "nce"

    def __init__(self) -> None:
        self.balance_lock = asyncio.Lock()
        self.buy_calls = 0

    async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
        return SupplierSnapshot(product_id, "Codex", "", 35_000, 10, 350_000)

    async def fetch_balance(self) -> int:
        return 350_000

    async def buy(
        self,
        product_id: str,
        quantity: int,
        *,
        idempotency_key: str | None = None,
    ) -> SupplierPurchase:
        self.buy_calls += 1
        return SupplierPurchase(
            order_code=f"NCE-ORDER-{self.buy_calls}",
            unit_price=35_000,
            accounts=(f"sk-customer-{self.buy_calls}",),
            product_id=product_id,
            provider="nce",
        )


def test_nce_wallet_purchase_records_cost_and_provider() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = WalletNceSupplier()
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

        result = await purchase_product(
            sessions,
            123,
            product.id,
            cipher,
            nce_client=supplier,  # type: ignore[arg-type]
        )

        assert result.ok
        assert supplier.buy_calls == 1
        async with sessions() as session:
            order = await session.scalar(select(Order))
            audit = await session.scalar(
                select(SupplierBalanceTransaction).where(
                    SupplierBalanceTransaction.provider == "nce"
                )
            )
            assert order is not None
            assert order.supplier_provider == "nce"
            assert order.cost_amount == 35_000
            assert audit is not None and audit.amount == -35_000
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
            fulfillment_source="nce",
            supplier_product_id="3",
        ),
        Product(
            id=2,
            category_id=3,
            name_vi="API CLAUDE - 50M token",
            name_en="CLAUDE API - 50M tokens",
            price=40_000,
            fulfillment_source="nce",
            supplier_product_id="1",
        ),
    ]
    quick = products_menu(products, "vi", "back:menu")
    quick_callbacks = [
        button.callback_data for row in quick.inline_keyboard for button in row
    ]
    assert quick_callbacks == ["prod:1", "prod:2", "back:menu"]
    assert not any(callback.startswith("quick:") for callback in quick_callbacks)

    family = nce_family_menu(3, "vi", products)
    family_callbacks = [
        button.callback_data for row in family.inline_keyboard for button in row
    ]
    assert "nce-family:3:codex" in family_callbacks
    assert "nce-family:3:claude" in family_callbacks
