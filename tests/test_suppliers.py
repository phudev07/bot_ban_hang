import asyncio
import hashlib
import hmac

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.models import Product
from app.suppliers import SumistoreClient, ensure_sumistore_product


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
        assert snapshot.unit_price == 15_000
        assert snapshot.source_stock == 100
        assert snapshot.effective_stock == 2

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


def test_sumistore_catalog_seeds_multiple_products() -> None:
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
                "SP-PAJWU273",
                "SP-GBKYZH09",
            ]
            assert [product.price for product in products] == [20_000, 6_000, 75_000, 85_000]
            assert all(product.external_stock == 0 for product in products)
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
