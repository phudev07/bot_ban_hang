import asyncio
import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.models import Category, InventoryItem, Product
from app.suppliers import SumistoreClient, ensure_sumistore_product, refresh_external_product


def test_sumistore_snapshot_uses_balance_limited_stock() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Tele-API-ID"] == "TAPI-test"
        if request.url.path.endswith("/tele-balance"):
            return httpx.Response(200, json={"success": True, "owner": {"balance": 30_000}})
        return httpx.Response(
            200,
            json={
                "success": True,
                "product": {
                    "id": "SP-GEF55PBV",
                    "name": "ChatGPT Plus",
                    "description": "Test",
                    "price": 15_000,
                    "stock": 100,
                },
            },
        )

    async def scenario() -> None:
        client = SumistoreClient(
            "https://supplier.test/api",
            "TAPI-test",
            transport=httpx.MockTransport(handler),
        )
        snapshot = await client.fetch_snapshot("SP-GEF55PBV")
        balance = await client.fetch_balance()
        assert snapshot.unit_price == 15_000
        assert snapshot.source_stock == 100
        assert snapshot.effective_stock == 2
        assert balance == 30_000

    asyncio.run(scenario())


def test_sumistore_stock_includes_recovered_local_inventory() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tele-balance"):
            return httpx.Response(200, json={"success": True, "owner": {"balance": 30_000}})
        return httpx.Response(
            200,
            json={
                "success": True,
                "product": {
                    "id": "SP-GEF55PBV",
                    "name": "ChatGPT Plus",
                    "description": "Test",
                    "price": 15_000,
                    "stock": 100,
                },
            },
        )

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        client = SumistoreClient(
            "https://supplier.test/api",
            "TAPI-test",
            transport=httpx.MockTransport(handler),
        )
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
            session.add(product)
            await session.flush()
            session.add(
                InventoryItem(
                    product_id=product.id,
                    encrypted_secret="encrypted",
                    supplier_order_code="API-RECOVERED",
                    supplier_item_index=0,
                )
            )
            await session.flush()

            stock = await refresh_external_product(session, product, client)

            assert stock == 3
            assert product.external_stock == 3
        await engine.dispose()

    asyncio.run(scenario())


def test_sumistore_purchase_signs_exact_body_and_extracts_accounts() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = (await request.aread()).decode()
        assert body == '{"id":"SP-GEF55PBV","quantity":2}'
        timestamp = request.headers["X-Timestamp"]
        nonce = request.headers["X-Nonce"]
        expected = hmac.new(
            b"TAPI-test",
            f"{timestamp}|{nonce}|{body}".encode(),
            hashlib.sha256,
        ).hexdigest()
        assert hmac.compare_digest(request.headers["X-Signature"], expected)
        return httpx.Response(
            200,
            json={
                "success": True,
                "order_code": "API-TELE-ABC123",
                "pricing": {"unit_price": 15_000},
                "raw_accounts": ["mail1|pass1", "mail2|pass2"],
            },
        )

    async def scenario() -> None:
        client = SumistoreClient(
            "https://supplier.test/api",
            "TAPI-test",
            transport=httpx.MockTransport(handler),
        )
        purchase = await client.buy("SP-GEF55PBV", 2)
        assert purchase.order_code == "API-TELE-ABC123"
        assert purchase.unit_price == 15_000
        assert purchase.accounts == ("mail1|pass1", "mail2|pass2")

    asyncio.run(scenario())


def test_sumistore_recovers_one_recent_unrecorded_order() -> None:
    started_at = datetime.now(UTC)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tele-orders"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "orders": [
                        {
                            "order_code": "API-OLD",
                            "product": {"id": "SP-GEF55PBV"},
                            "quantity": 2,
                            "created_at": (started_at - timedelta(minutes=1)).isoformat(),
                        },
                        {
                            "order_code": "API-RECOVERED",
                            "product": {"id": "SP-GEF55PBV"},
                            "quantity": 2,
                            "created_at": (started_at + timedelta(seconds=1)).isoformat(),
                        },
                    ],
                },
            )
        assert request.url.path.endswith("/tele-orders/API-RECOVERED")
        return httpx.Response(
            200,
            json={
                "success": True,
                "order": {
                    "order_code": "API-RECOVERED",
                    "product": {"id": "SP-GEF55PBV"},
                    "quantity": 2,
                    "total_amount": 30_000,
                    "raw_accounts": ["mail1|pass1", "mail2|pass2"],
                },
            },
        )

    async def scenario() -> None:
        client = SumistoreClient(
            "https://supplier.test/api",
            "TAPI-test",
            transport=httpx.MockTransport(handler),
        )
        recovered = await client.recover_recent_purchase(
            "SP-GEF55PBV",
            2,
            started_at=started_at,
            known_order_codes={"API-OLD"},
        )

        assert recovered is not None
        assert recovered.order_code == "API-RECOVERED"
        assert recovered.unit_price == 15_000
        assert recovered.accounts == ("mail1|pass1", "mail2|pass2")

    asyncio.run(scenario())


def test_sumistore_catalog_only_seeds_supported_gpt_products() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        settings = Settings(
            _env_file=None,
            bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
            inventory_encryption_key=Fernet.generate_key().decode(),
            sepay_enabled=False,
            SUMISTORE_PRODUCT_IDS=(
                "SP-GEF55PBV,SP-JMYJL2PL,SP-PAJWU273,SP-GBKYZH09"
            ),
        )

        await ensure_sumistore_product(sessions, settings)

        async with sessions() as session:
            products = list(await session.scalars(select(Product).order_by(Product.id)))
            assert [product.supplier_product_id for product in products] == [
                "SP-GEF55PBV",
                "SP-JMYJL2PL",
            ]
            assert [product.price for product in products] == [20_000, 6_000]
            assert all(product.external_stock == 0 for product in products)
        await engine.dispose()

    asyncio.run(scenario())


def test_sumistore_catalog_deactivates_products_outside_supported_list() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        settings = Settings(
            _env_file=None,
            bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
            inventory_encryption_key=Fernet.generate_key().decode(),
            sepay_enabled=False,
        )

        async with sessions() as session:
            category = Category(name_vi="Đã ngừng bán", name_en="Discontinued")
            session.add(category)
            await session.flush()
            session.add(
                Product(
                    category_id=category.id,
                    name_vi="Gemini cũ",
                    name_en="Old Gemini",
                    price=75_000,
                    fulfillment_source="sumistore",
                    supplier_product_id="SP-PAJWU273",
                    external_stock=9,
                )
            )
            await session.commit()

        await ensure_sumistore_product(sessions, settings)

        async with sessions() as session:
            discontinued = await session.scalar(
                select(Product).where(Product.supplier_product_id == "SP-PAJWU273")
            )
            assert discontinued is not None
            assert discontinued.active is False
            assert discontinued.external_stock == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_sumistore_catalog_preserves_custom_product_markup() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        settings = Settings(
            _env_file=None,
            bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
            inventory_encryption_key=Fernet.generate_key().decode(),
            sepay_enabled=False,
            SUMISTORE_PRODUCT_IDS="SP-JMYJL2PL",
        )

        await ensure_sumistore_product(sessions, settings)

        async with sessions() as session:
            product = await session.scalar(select(Product))
            assert product is not None
            product.supplier_markup = 2_000
            product.supplier_price = 1_000
            product.price = 3_000
            await session.commit()

        await ensure_sumistore_product(sessions, settings)

        async with sessions() as session:
            product = await session.scalar(select(Product))
            assert product is not None
            assert product.supplier_markup == 2_000
            assert product.supplier_price == 1_000
            assert product.price == 3_000
        await engine.dispose()

    asyncio.run(scenario())
