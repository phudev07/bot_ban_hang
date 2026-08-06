import asyncio
import json

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.haji_suppliers import HajiClient, ensure_haji_products, haji_product_kind
from app.models import (
    Category,
    Product,
    SupplierBalanceTransaction,
    SupplierPurchaseAttempt,
    User,
)
from app.services import buy_supplier_product, purchase_product
from app.suppliers import SupplierPurchase, SupplierSnapshot
from app.utils import SecretCipher


def catalog_payload() -> dict[str, object]:
    return {
        "ok": True,
        "data": {
            "products": [
                {
                    "product_id": "netflix_4k",
                    "name": "Netflix 4K Premium",
                    "price": 20_000,
                    "currency": "VND",
                    "stock_count": 8,
                    "available": True,
                    "description": "Bảo hành 30 ngày",
                },
                {
                    "product_id": "gpt_gcash_1m",
                    "name": "GPT Plus GCash",
                    "price": 30_000,
                    "currency": "VND",
                    "stock_count": 5,
                    "available": True,
                    "description": "Giao ngay",
                },
                {
                    "product_id": "chatgpt_k12",
                    "name": "ChatGPT K12",
                    "price": 15_000,
                    "currency": "VND",
                    "stock_count": 10,
                    "available": True,
                    "description": "Giao ngay",
                },
                {
                    "product_id": "other_product",
                    "name": "Other account",
                    "price": 1_000,
                    "stock_count": 100,
                },
            ],
            "total_products": 4,
        },
    }


def test_haji_product_matching_is_limited_to_requested_families() -> None:
    assert haji_product_kind("Netflix 4K Premium") == "netflix"
    assert haji_product_kind("GPT Plus GCash") == "gpt_gcash"
    assert haji_product_kind("GPT K12") == "gpt_k12"
    assert haji_product_kind("KBH12") == "gpt_k12"
    assert haji_product_kind("12K ChatGPT") == "gpt_k12"
    assert haji_product_kind("ChatGPT Go 1 month") is None
    assert haji_product_kind("ChatGPT Plus iCloud") is None


def test_haji_catalog_balance_and_bulk_purchase_use_documented_contract() -> None:
    seen_order_headers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == "dl_test_key_123456789"
        if request.url.path == "/api/v2/catalog":
            assert request.url.params["available_only"] == "false"
            assert request.url.params["limit"] == "500"
            return httpx.Response(200, json=catalog_payload())
        if request.url.path == "/api/v2/me":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {"balance": 100_000, "currency": "VND"},
                },
            )
        if request.url.path == "/api/v2/orders":
            seen_order_headers.append(request.headers["x-idempotency-key"])
            body = json.loads(request.content)
            assert body == {
                "product_id": "netflix_4k",
                "quantity": 2,
                "partner_ref": "shop-order-001",
            }
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "order_code": "AP-TEST001",
                        "quantity": 2,
                        "unit_price": 20_000,
                        "total_price": 40_000,
                        "balance": 60_000,
                        "items": [
                            {"value": "netflix-1|pass", "type": "account"},
                            {"value": "netflix-2|pass", "type": "account"},
                        ],
                    },
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async def scenario() -> None:
        client = HajiClient(
            "https://api.haji.in.net",
            "dl_test_key_123456789",
            transport=httpx.MockTransport(handler),
        )
        products = await client.refresh_catalog(force=True)
        assert [product.kind for product in products] == [
            "netflix",
            "gpt_gcash",
            "gpt_k12",
        ]
        snapshot = await client.fetch_snapshot("netflix_4k")
        assert snapshot.unit_price == 20_000
        assert snapshot.effective_stock == 5
        gcash_snapshot = await client.fetch_snapshot("gpt_gcash_1m")
        assert gcash_snapshot.effective_stock == 3
        purchase = await client.buy(
            "netflix_4k",
            2,
            idempotency_key="shop-order-001",
        )
        assert purchase.order_code == "HAJI-AP-TEST001"
        assert purchase.accounts == ("netflix-1|pass", "netflix-2|pass")
        assert purchase.provider == "haji"
        assert seen_order_headers == ["shop-order-001"]
        await client.aclose()

    asyncio.run(scenario())


def test_haji_transient_catalog_failure_keeps_last_known_products_active() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"ok": False, "detail": "maintenance"})

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            category = Category(name_vi="Netflix", name_en="Netflix")
            session.add(category)
            await session.flush()
            session.add(
                Product(
                    category_id=category.id,
                    name_vi="Netflix 4K Premium",
                    name_en="Netflix 4K Premium",
                    price=25_000,
                    fulfillment_source="haji",
                    supplier_product_id="netflix_4k",
                    external_stock=4,
                    active=True,
                )
            )
            await session.commit()
        client = HajiClient(
            "https://api.haji.in.net",
            "sk-test-key",
            transport=httpx.MockTransport(handler),
        )
        await ensure_haji_products(sessions, client, markup=5_000)
        async with sessions() as session:
            product = await session.scalar(
                select(Product).where(Product.fulfillment_source == "haji")
            )
            assert product is not None and product.active is True
            assert product.external_stock == 4
        await client.aclose()
        await engine.dispose()

    asyncio.run(scenario())


def test_haji_retry_reuses_the_same_idempotency_key() -> None:
    post_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/v2/orders":
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")
        post_keys.append(request.headers["x-idempotency-key"])
        if len(post_keys) == 1:
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": {
                    "order_code": "AP-RETRY001",
                    "quantity": 1,
                    "unit_price": 20_000,
                    "total_price": 20_000,
                    "balance": 80_000,
                    "items": [{"value": "netflix|pass", "type": "account"}],
                },
            },
        )

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        client = HajiClient(
            "https://api.haji.in.net",
            "dl_test_key_123456789",
            transport=httpx.MockTransport(handler),
        )
        async with sessions() as session:
            purchase = await buy_supplier_product(
                session,
                client,
                "netflix_4k",
                1,
                idempotency_key="shop-retry-001",
            )
            await session.commit()
            attempt = await session.scalar(select(SupplierPurchaseAttempt))
            assert purchase.order_code == "HAJI-AP-RETRY001"
            assert attempt is not None and attempt.status == "succeeded"
            assert attempt.supplier_order_code == "HAJI-AP-RETRY001"
        assert post_keys == ["shop-retry-001", "shop-retry-001"]
        await client.aclose()
        await engine.dispose()

    asyncio.run(scenario())


def test_haji_products_are_imported_into_netflix_and_existing_gpt_categories() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/catalog":
            return httpx.Response(200, json=catalog_payload())
        if request.url.path == "/api/v2/me":
            return httpx.Response(
                200,
                json={"ok": True, "data": {"balance": 100_000}},
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            session.add(Category(name_vi="ChatGPT", name_en="ChatGPT", position=1))
            await session.commit()
        client = HajiClient(
            "https://api.haji.in.net",
            "dl_test_key_123456789",
            transport=httpx.MockTransport(handler),
        )
        await ensure_haji_products(sessions, client, markup=5_000)
        async with sessions() as session:
            rows = (
                await session.execute(
                    select(Product, Category)
                    .join(Category, Category.id == Product.category_id)
                    .where(Product.fulfillment_source == "haji")
                    .order_by(Product.supplier_product_id)
                )
            ).all()
            assert len(rows) == 3
            by_id = {product.supplier_product_id: (product, category) for product, category in rows}
            netflix, netflix_category = by_id["netflix_4k"]
            assert netflix.price == 25_000
            assert netflix.allow_quantity is True and netflix.max_quantity == 100
            assert netflix_category.name_vi == "Netflix"
            assert by_id["gpt_gcash_1m"][1].name_vi == "ChatGPT"
            assert by_id["chatgpt_k12"][1].name_vi == "ChatGPT"
        await client.aclose()
        await engine.dispose()

    asyncio.run(scenario())


class HajiBuyingSupplier:
    provider = "haji"

    def __init__(self) -> None:
        self.balance_lock = asyncio.Lock()

    async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
        return SupplierSnapshot(
            product_id=product_id,
            name="Netflix 4K Premium",
            description="",
            unit_price=20_000,
            source_stock=10,
            owner_balance=200_000,
        )

    async def fetch_balance(self) -> int:
        return 200_000

    async def buy(
        self,
        product_id: str,
        quantity: int,
        *,
        idempotency_key: str | None = None,
    ) -> SupplierPurchase:
        assert idempotency_key
        return SupplierPurchase(
            order_code="HAJI-AP-WALLET001",
            unit_price=20_000,
            accounts=tuple(f"netflix-{index}|pass" for index in range(quantity)),
            product_id=product_id,
            provider=self.provider,
        )


def test_haji_wallet_purchase_records_actual_cost_provider_and_one_batch() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="Netflix", name_en="Netflix")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Netflix 4K Premium",
                name_en="Netflix 4K Premium",
                price=25_000,
                allow_quantity=True,
                max_quantity=100,
                fulfillment_source="haji",
                supplier_product_id="netflix_4k",
                supplier_markup=5_000,
                supplier_price=20_000,
                external_stock=10,
            )
            user = User(telegram_id=123, full_name="Buyer", balance=100_000)
            session.add_all([product, user])
            await session.commit()
            product_id = product.id

        result = await purchase_product(
            sessions,
            123,
            product_id,
            cipher,
            quantity=2,
            haji_client=HajiBuyingSupplier(),  # type: ignore[arg-type]
        )
        assert result.ok is True
        assert len(result.orders) == 2
        assert len({order.batch_code for order in result.orders}) == 1
        assert all(order.supplier_provider == "haji" for order in result.orders)
        assert all(order.cost_amount == 20_000 for order in result.orders)
        async with sessions() as session:
            audit = await session.scalar(
                select(SupplierBalanceTransaction).where(
                    SupplierBalanceTransaction.provider == "haji"
                )
            )
            assert audit is not None and audit.amount == -40_000
            assert audit.quantity == 2
        await engine.dispose()

    asyncio.run(scenario())
