import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.lehai_suppliers import (
    CATEGORY_VI,
    LeHaiPremiumClient,
    ensure_lehai_products,
    refresh_lehai_product,
    sync_lehai_products,
)
from app.models import (
    Category,
    Deposit,
    InventoryItem,
    Order,
    Product,
    ProductStockAlert,
    SupplierBalanceTransaction,
    SupplierPurchaseAttempt,
    User,
)
from app.services import buy_supplier_product, process_sepay_payment, purchase_product
from app.suppliers import SupplierError, SupplierPurchase, SupplierSnapshot
from app.utils import SecretCipher


def product_payload() -> dict[str, object]:
    return {
        "success": True,
        "walletCurrency": "VND",
        "products": [
            {
                "_id": "cdk_pixel",
                "product_name": "CDK GG Pro Pixel 1 Năm",
                "walletPricing": 25_000,
                "description": "Pixel offer key",
                "stats": {"available": 203},
            },
            {
                "_id": "cdk_ggpro_18m",
                "product_name": "Link GG Pro Jio 18M",
                "walletPricing": 27_000,
                "description": "Jio family link",
                "stats": {"available": 260},
            },
            {
                "_id": "gptupi_kbh12k",
                "product_name": "BHF GPT PLUS GMAIL APPLE PAY",
                "walletPricing": 130_000,
                "description": "Gmail Apple Pay account",
                "stats": {"available": 11},
            },
        ],
    }


def test_lehai_snapshot_limits_stock_by_wallet_balance() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == "tgb_test"
        assert "key" not in request.url.params
        if request.url.path.endswith("/balance"):
            return httpx.Response(200, json={"success": True, "balance": 100_000})
        return httpx.Response(200, json=product_payload())

    async def scenario() -> None:
        client = LeHaiPremiumClient(
            "https://supplier.test",
            "tgb_test",
            transport=httpx.MockTransport(handler),
        )
        snapshot = await client.fetch_snapshot("cdk_pixel")

        assert snapshot.unit_price == 25_000
        assert snapshot.source_stock == 203
        assert snapshot.owner_balance == 100_000
        assert snapshot.effective_stock == 4

    asyncio.run(scenario())


def test_lehai_snapshot_cache_reuses_product_list_and_connection() -> None:
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("/balance"):
            return httpx.Response(200, json={"success": True, "balance": 200_000})
        return httpx.Response(200, json=product_payload())

    async def scenario() -> None:
        client = LeHaiPremiumClient(
            "https://supplier.test",
            "tgb_test",
            snapshot_cache_seconds=10,
            transport=httpx.MockTransport(handler),
        )
        pixel = await client.fetch_snapshot("cdk_pixel")
        jio = await client.fetch_snapshot("cdk_ggpro_18m")
        first_http_client = client._http_client

        assert pixel.unit_price == 25_000
        assert jio.unit_price == 27_000
        assert len(requests) == 2
        assert first_http_client is not None
        assert client._http_client is first_http_client

        await client.aclose()
        assert first_http_client.is_closed

    asyncio.run(scenario())


def test_lehai_jio_uses_sale_id_then_returns_to_canonical_id() -> None:
    sale_active = True
    purchased_product_ids: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sale_active
        if request.url.path.endswith("/balance"):
            return httpx.Response(200, json={"success": True, "balance": 100_000})
        if request.url.path.endswith("/products"):
            payload = product_payload()
            if sale_active:
                payload["products"].append(  # type: ignore[union-attr]
                    {
                        "_id": "sale_link18mgemini",
                        "product_name": "[SALE] Link GG Pro Jio 18M",
                        "walletPricing": 20_000,
                        "description": "Temporary Jio sale",
                        "stats": {"available": 9_999},
                    }
                )
            return httpx.Response(200, json=payload)
        body = json.loads((await request.aread()).decode())
        purchased_product_ids.append(body["product_id"])
        unit_price = 20_000 if body["product_id"] == "sale_link18mgemini" else 27_000
        return httpx.Response(
            200,
            json={
                "success": True,
                "orderCode": f"ORDER-{len(purchased_product_ids)}",
                "amount": unit_price,
                "deliveredAccounts": [{"user": f"https://offer.test/{len(purchased_product_ids)}"}],
            },
        )

    async def scenario() -> None:
        nonlocal sale_active
        client = LeHaiPremiumClient(
            "https://supplier.test",
            "tgb_test",
            transport=httpx.MockTransport(handler),
        )

        sale_snapshot = await client.fetch_snapshot("cdk_ggpro_18m")
        sale_purchase = await client.buy(
            "cdk_ggpro_18m",
            1,
            idempotency_key="sale-order",
        )
        assert sale_snapshot.product_id == "cdk_ggpro_18m"
        assert sale_snapshot.unit_price == 20_000
        assert sale_snapshot.source_stock == 9_999
        assert sale_purchase.product_id == "cdk_ggpro_18m"
        assert sale_purchase.unit_price == 20_000

        sale_active = False
        standard_snapshot = await client.fetch_snapshot("cdk_ggpro_18m")
        standard_purchase = await client.buy(
            "cdk_ggpro_18m",
            1,
            idempotency_key="standard-order",
        )
        assert standard_snapshot.product_id == "cdk_ggpro_18m"
        assert standard_snapshot.unit_price == 27_000
        assert standard_purchase.product_id == "cdk_ggpro_18m"
        assert standard_purchase.unit_price == 27_000
        assert purchased_product_ids == ["sale_link18mgemini", "cdk_ggpro_18m"]
        await client.aclose()

    asyncio.run(scenario())


def test_lehai_sale_500_opens_circuit_and_stops_followup_requests() -> None:
    catalog_requests = 0
    balance_requests = 0
    purchase_requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal catalog_requests, balance_requests, purchase_requests
        if request.url.path.endswith("/balance"):
            balance_requests += 1
            return httpx.Response(200, json={"success": True, "balance": 100_000})
        if request.url.path.endswith("/products"):
            catalog_requests += 1
            payload = product_payload()
            payload["products"].append(  # type: ignore[union-attr]
                {
                    "_id": "sale_link18mgemini",
                    "product_name": "[SALE] Link GG Pro Jio 18M",
                    "walletPricing": 20_000,
                    "description": "Temporary Jio sale",
                    "stats": {"available": 9_999},
                }
            )
            return httpx.Response(200, json=payload)
        purchase_requests += 1
        return httpx.Response(
            500,
            json={"success": False, "message": "purchase failed"},
        )

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())
        client = LeHaiPremiumClient(
            "https://supplier.test",
            "tgb_test",
            transport=httpx.MockTransport(handler),
        )
        async with sessions() as session:
            category = Category(name_vi=CATEGORY_VI, name_en=CATEGORY_VI)
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Link GG Pro Jio 18M",
                name_en="Google Pro Jio 18M Link",
                price=28_000,
                allow_quantity=True,
                max_quantity=100,
                fulfillment_source="lehai",
                supplier_product_id="cdk_ggpro_18m",
                supplier_markup=8_000,
                external_stock=5,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=100_000)
            session.add_all([product, user])
            await session.commit()

        first = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            1,
            lehai_client=client,
            supplier_idempotency_key="sale-failure-first",
        )
        await client.aclose()
        restarted_client = LeHaiPremiumClient(
            "https://supplier.test",
            "tgb_test",
            transport=httpx.MockTransport(handler),
        )
        second = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            1,
            lehai_client=restarted_client,
            supplier_idempotency_key="sale-failure-second",
        )

        assert first.ok is False and first.message == "supplier_unavailable"
        assert second.ok is False and second.message == "out_of_stock"
        assert purchase_requests == 1
        assert catalog_requests == 1
        assert balance_requests == 1
        assert restarted_client.purchase_is_blocked("cdk_ggpro_18m") is True
        async with sessions() as session:
            stored_product = await session.get(Product, product.id)
            attempts = list(
                await session.scalars(
                    select(SupplierPurchaseAttempt).order_by(SupplierPurchaseAttempt.id)
                )
            )
            stored_user = await session.get(User, user.telegram_id)
            assert stored_product is not None
            assert stored_product.external_stock == 0
            assert stored_product.supplier_available_stock == 0
            assert len(attempts) == 1
            assert attempts[0].error_code == "SUPPLIER_HTTP_500"
            assert stored_user is not None and stored_user.balance == 100_000
            attempts[0].completed_at = datetime.now(UTC) - timedelta(minutes=11)
            await session.commit()

        # Simulate the ten-minute TTL expiring: the next sync may expose the
        # product again, and only a new failed purchase can reopen the circuit.
        restarted_client._purchase_backoff_until["cdk_ggpro_18m"] = 0.0
        async with sessions() as session:
            stored_product = await session.get(Product, product.id)
            assert stored_product is not None
            assert await refresh_lehai_product(
                session,
                stored_product,
                restarted_client,
            ) == 5
            await session.commit()
        assert catalog_requests == 2
        assert balance_requests == 2
        await restarted_client.aclose()
        await engine.dispose()

    asyncio.run(scenario())


def test_lehai_temporary_refresh_failure_keeps_last_known_stock() -> None:
    class UnavailableSupplier:
        async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
            raise SupplierError("SUPPLIER_UNAVAILABLE")

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            category = Category(name_vi=CATEGORY_VI, name_en=CATEGORY_VI)
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Link GG Pro Jio 18M",
                name_en="Link GG Pro Jio 18M",
                price=35_000,
                fulfillment_source="lehai",
                supplier_product_id="cdk_ggpro_18m",
                external_stock=6,
                supplier_available_stock=6,
                supplier_available_stock_initialized=True,
            )
            session.add(product)
            await session.flush()

            stock = await refresh_lehai_product(
                session,
                product,
                UnavailableSupplier(),  # type: ignore[arg-type]
            )

            assert stock == 6
            assert product.external_stock == 6
            assert product.supplier_available_stock == 6
        await engine.dispose()

    asyncio.run(scenario())


def test_lehai_price_lock_includes_supplier_stock() -> None:
    class AvailableSupplier:
        async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
            return SupplierSnapshot(
                product_id=product_id,
                name="Link GG Pro Jio 18M",
                description="Test",
                unit_price=25_000,
                source_stock=100,
                owner_balance=250_000,
            )

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            category = Category(name_vi=CATEGORY_VI, name_en=CATEGORY_VI)
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Link GG Pro Jio 18M",
                name_en="Link GG Pro Jio 18M",
                price=28_000,
                fulfillment_source="lehai",
                supplier_product_id="cdk_ggpro_18m",
                supplier_price=20_000,
                supplier_markup=8_000,
                price_lock_enabled=True,
            )
            session.add(product)
            await session.flush()
            session.add(
                InventoryItem(
                    product_id=product.id,
                    encrypted_secret="encrypted",
                    cost_amount=20_000,
                )
            )
            await session.flush()

            stock = await refresh_lehai_product(
                session,
                product,
                AvailableSupplier(),  # type: ignore[arg-type]
            )
            await session.commit()

            assert stock == 11
            assert product.external_stock == 11
            assert product.supplier_available_stock == 10
            assert product.supplier_price == 25_000
            assert product.price == 28_000
            assert product.price_lock_enabled is True
        await engine.dispose()

    asyncio.run(scenario())


def test_lehai_missing_product_uses_refresh_backoff() -> None:
    request_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request.url.path.endswith("/balance"):
            return httpx.Response(200, json={"success": True, "balance": 100_000})
        payload = product_payload()
        payload["products"] = [
            product
            for product in payload["products"]
            if product["_id"] != "cdk_ggpro_18m"
        ]
        return httpx.Response(200, json=payload)

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        client = LeHaiPremiumClient(
            "https://supplier.test",
            "tgb_test",
            snapshot_cache_seconds=1,
            transport=httpx.MockTransport(handler),
        )
        async with sessions() as session:
            category = Category(name_vi=CATEGORY_VI, name_en=CATEGORY_VI)
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Missing Jio",
                name_en="Missing Jio",
                price=35_000,
                fulfillment_source="lehai",
                supplier_product_id="cdk_ggpro_18m",
                external_stock=4,
            )
            session.add(product)
            await session.flush()

            assert await refresh_lehai_product(session, product, client) == 0
            assert request_count == 2
            client.invalidate_snapshot_cache()
            assert await refresh_lehai_product(session, product, client) == 0
            assert request_count == 2

        await client.aclose()
        await engine.dispose()

    asyncio.run(scenario())


def test_lehai_jio_stock_alert_requires_a_real_wallet_topup() -> None:
    class BalanceSupplier:
        def __init__(self, balance: int) -> None:
            self.balance = balance

        async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
            return SupplierSnapshot(
                product_id=product_id,
                name="Link GG Pro Jio 18M",
                description="Jio family link",
                unit_price=27_000,
                source_stock=79,
                owner_balance=self.balance,
            )

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async with sessions() as session:
            category = Category(name_vi=CATEGORY_VI, name_en=CATEGORY_VI)
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Link GG Pro Jio 18M",
                name_en="Google Pro Jio 18M Link",
                price=35_000,
                fulfillment_source="lehai",
                supplier_product_id="cdk_ggpro_18m",
                supplier_markup=8_000,
                supplier_price=27_000,
                external_stock=4,
                supplier_available_stock=4,
                supplier_available_stock_initialized=True,
                supplier_owner_balance=108_000,
                supplier_synced_at=datetime.now(UTC),
            )
            session.add(product)
            await session.commit()

            await refresh_lehai_product(
                session,
                product,
                BalanceSupplier(135_000),  # type: ignore[arg-type]
            )
            await session.commit()
            assert await session.scalar(select(ProductStockAlert.id)) is not None

        async with sessions() as session:
            await session.execute(ProductStockAlert.__table__.delete())
            product = await session.scalar(
                select(Product).where(Product.supplier_product_id == "cdk_ggpro_18m")
            )
            assert product is not None
            product.external_stock = 4
            product.supplier_available_stock = 4
            product.supplier_owner_balance = 108_000
            session.add(
                SupplierBalanceTransaction(
                    provider="lehai",
                    kind="suspicious",
                    amount=-27_000,
                    balance_before=135_000,
                    balance_after=108_000,
                )
            )
            await session.commit()

            await refresh_lehai_product(
                session,
                product,
                BalanceSupplier(135_000),  # type: ignore[arg-type]
            )
            await session.commit()
            assert await session.scalar(select(ProductStockAlert.id)) is None

            product.external_stock = 4
            product.supplier_available_stock = 4
            product.supplier_owner_balance = 135_000
            product.notify_stock_without_balance_topup = True
            await refresh_lehai_product(
                session,
                product,
                BalanceSupplier(135_000),  # type: ignore[arg-type]
            )
            await session.commit()
            alert = await session.scalar(select(ProductStockAlert))
            assert alert is not None
            assert alert.stock_before == 4
            assert alert.stock_after == 5

        await engine.dispose()

    asyncio.run(scenario())


def test_lehai_topup_notifies_gpt_plus_when_it_is_the_selected_route() -> None:
    class RouteSupplier:
        def __init__(self, provider: str, price: int, balance: int) -> None:
            self.provider = provider
            self.price = price
            self.balance = balance

        async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
            if product_id == "cdk_ggpro_18m":
                return SupplierSnapshot(
                    product_id=product_id,
                    name="Link GG Pro Jio 18M",
                    description="",
                    unit_price=27_000,
                    source_stock=100,
                    owner_balance=self.balance,
                )
            return SupplierSnapshot(
                product_id=product_id,
                name="GPT Plus",
                description="",
                unit_price=self.price,
                source_stock=100,
                owner_balance=self.balance,
            )

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        sumi = RouteSupplier("sumistore", 30_000, 300_000)
        lehai = RouteSupplier("lehai", 25_000, 150_000)

        async with sessions() as session:
            category = Category(name_vi="Products", name_en="Products")
            session.add(category)
            await session.flush()
            jio = Product(
                category_id=category.id,
                name_vi="Link GG Pro Jio 18M",
                name_en="Google Pro Jio 18M Link",
                price=35_000,
                fulfillment_source="lehai",
                supplier_product_id="cdk_ggpro_18m",
                supplier_markup=8_000,
                supplier_price=27_000,
                external_stock=3,
                supplier_available_stock=3,
                supplier_available_stock_initialized=True,
                supplier_owner_balance=100_000,
                supplier_synced_at=datetime.now(UTC),
            )
            gpt = Product(
                category_id=category.id,
                name_vi="GPT Plus",
                name_en="GPT Plus",
                price=35_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_markup=5_000,
                supplier_price=30_000,
                external_stock=14,
                supplier_available_stock=14,
                supplier_available_stock_initialized=True,
                supplier_owner_balance=300_000,
                supplier_synced_at=datetime.now(UTC),
            )
            session.add_all([jio, gpt])
            await session.commit()

            await refresh_lehai_product(
                session,
                jio,
                lehai,  # type: ignore[arg-type]
                sumistore_client=sumi,  # type: ignore[arg-type]
            )
            await session.commit()

            alerts = list(
                await session.scalars(select(ProductStockAlert).order_by(ProductStockAlert.id))
            )
            assert len(alerts) == 1
            assert alerts[0].product_id == gpt.id
            assert alerts[0].provider == "lehai"
            assert alerts[0].stock_before == 14
            assert alerts[0].stock_after == 16

        await engine.dispose()

    asyncio.run(scenario())


def test_lehai_topup_keeps_jio_alert_when_sumi_is_preferred_for_gpt_plus() -> None:
    class RouteSupplier:
        def __init__(self, provider: str, price: int, balance: int) -> None:
            self.provider = provider
            self.price = price
            self.balance = balance

        async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
            if product_id == "cdk_ggpro_18m":
                return SupplierSnapshot(
                    product_id=product_id,
                    name="Link GG Pro Jio 18M",
                    description="",
                    unit_price=27_000,
                    source_stock=100,
                    owner_balance=self.balance,
                )
            return SupplierSnapshot(
                product_id=product_id,
                name="GPT Plus",
                description="",
                unit_price=self.price,
                source_stock=100,
                owner_balance=self.balance,
            )

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        sumi = RouteSupplier("sumistore", 25_000, 250_000)
        lehai = RouteSupplier("lehai", 25_000, 150_000)

        async with sessions() as session:
            category = Category(name_vi="Products", name_en="Products")
            session.add(category)
            await session.flush()
            jio = Product(
                category_id=category.id,
                name_vi="Link GG Pro Jio 18M",
                name_en="Google Pro Jio 18M Link",
                price=35_000,
                fulfillment_source="lehai",
                supplier_product_id="cdk_ggpro_18m",
                supplier_markup=8_000,
                supplier_price=27_000,
                external_stock=3,
                supplier_available_stock=3,
                supplier_available_stock_initialized=True,
                supplier_owner_balance=100_000,
                supplier_synced_at=datetime.now(UTC),
            )
            gpt = Product(
                category_id=category.id,
                name_vi="GPT Plus",
                name_en="GPT Plus",
                price=30_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_markup=5_000,
                supplier_price=25_000,
                external_stock=14,
                supplier_available_stock=14,
                supplier_available_stock_initialized=True,
                supplier_owner_balance=250_000,
                supplier_synced_at=datetime.now(UTC),
            )
            session.add_all([jio, gpt])
            await session.commit()

            await refresh_lehai_product(
                session,
                jio,
                lehai,  # type: ignore[arg-type]
                sumistore_client=sumi,  # type: ignore[arg-type]
            )
            await session.commit()

            alerts = list(
                await session.scalars(select(ProductStockAlert).order_by(ProductStockAlert.id))
            )
            assert len(alerts) == 1
            assert alerts[0].product_id == jio.id
            assert alerts[0].provider == "lehai"
            assert alerts[0].stock_before == 3
            assert alerts[0].stock_after == 5

        await engine.dispose()

    asyncio.run(scenario())


def test_lehai_purchase_uses_idempotency_and_extracts_delivered_items() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads((await request.aread()).decode())
        assert body == {
            "product_id": "cdk_ggpro_18m",
            "quantity": 2,
            "idempotency_key": "shop-order-123",
        }
        assert request.headers["X-API-Key"] == "tgb_test"
        return httpx.Response(
            200,
            json={
                "success": True,
                "orderCode": "ORDER-123",
                "amount": 54_000,
                "deliveredAccounts": [
                    {"productItemId": "1", "user": "https://offer.test/one"},
                    {"productItemId": "2", "user": "https://offer.test/two"},
                ],
            },
        )

    async def scenario() -> None:
        client = LeHaiPremiumClient(
            "https://supplier.test",
            "tgb_test",
            transport=httpx.MockTransport(handler),
        )
        purchase = await client.buy(
            "cdk_ggpro_18m",
            2,
            idempotency_key="shop-order-123",
        )

        assert purchase.order_code == "LHP-ORDER-123"
        assert purchase.unit_price == 27_000
        assert purchase.accounts == (
            "https://offer.test/one",
            "https://offer.test/two",
        )
        assert purchase.provider == "lehai"

    asyncio.run(scenario())


def test_lehai_client_preserves_v14_error_codes_from_body_or_header() -> None:
    async def scenario() -> None:
        responses = [
            httpx.Response(
                401,
                headers={"X-Error-Code": "INVALID_API_KEY"},
                json={"detail": "invalid api key", "errorCode": "INVALID_API_KEY"},
            ),
            httpx.Response(
                403,
                json={
                    "detail": "channel required",
                    "errorCode": "CHANNEL_MEMBERSHIP_REQUIRED",
                },
            ),
        ]

        async def handler(request: httpx.Request) -> httpx.Response:
            return responses.pop(0)

        client = LeHaiPremiumClient(
            "https://supplier.test",
            "tgb_test",
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(SupplierError, match="invalid api key") as invalid_key:
            await client.fetch_balance()
        assert invalid_key.value.code == "INVALID_API_KEY"
        with pytest.raises(SupplierError, match="channel required") as membership:
            await client.fetch_products()
        assert membership.value.code == "CHANNEL_MEMBERSHIP_REQUIRED"

    asyncio.run(scenario())


def test_lehai_balance_mismatch_retries_with_same_idempotency_key() -> None:
    class BalanceMismatchSupplier:
        provider = "lehai"

        def __init__(self) -> None:
            self.calls: list[str | None] = []

        async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
            return SupplierSnapshot(
                product_id=product_id,
                name="Link GG Pro Jio 18M",
                description="Jio family link",
                unit_price=27_000,
                source_stock=100,
                owner_balance=173_000,
            )

        async def buy(
            self,
            product_id: str,
            quantity: int,
            *,
            idempotency_key: str | None = None,
        ) -> SupplierPurchase:
            self.calls.append(idempotency_key)
            if len(self.calls) == 1:
                raise SupplierError("INSUFFICIENT_BALANCE", "stale provider balance")
            return SupplierPurchase(
                order_code="LHP-RETRY-OK",
                unit_price=27_000,
                accounts=("https://offer.test/retried",),
                product_id=product_id,
                provider=self.provider,
            )

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        supplier = BalanceMismatchSupplier()
        async with sessions() as session:
            purchase = await buy_supplier_product(
                session,
                supplier,  # type: ignore[arg-type]
                "cdk_ggpro_18m",
                1,
                idempotency_key="tg-callback-retry",
            )
            assert purchase.accounts == ("https://offer.test/retried",)
            assert supplier.calls == ["tg-callback-retry", "tg-callback-retry"]
            attempt = await session.scalar(select(SupplierPurchaseAttempt))
            assert attempt is not None
            assert attempt.provider == "lehai"
            assert attempt.request_key == "tg-callback-retry"
            assert attempt.status == "succeeded"
            assert attempt.supplier_order_code == "LHP-RETRY-OK"
            assert attempt.error_code is None
        await engine.dispose()

    asyncio.run(scenario())


def test_lehai_failed_purchase_keeps_request_key_and_error_for_support() -> None:
    class FailedSupplier:
        provider = "lehai"

        async def buy(
            self,
            product_id: str,
            quantity: int,
            *,
            idempotency_key: str | None = None,
        ) -> SupplierPurchase:
            raise SupplierError("SUPPLIER_HTTP_500", "provider temporarily failed")

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            with pytest.raises(SupplierError):
                await buy_supplier_product(
                    session,
                    FailedSupplier(),  # type: ignore[arg-type]
                    "cdk_ggpro_18m",
                    1,
                    idempotency_key="support-trace-001",
                )
            attempt = await session.scalar(select(SupplierPurchaseAttempt))
            assert attempt is not None
            assert attempt.request_key == "support-trace-001"
            assert attempt.status == "failed"
            assert attempt.error_code == "SUPPLIER_HTTP_500"
            assert attempt.error_detail == "provider temporarily failed"
            assert attempt.supplier_order_code is None
        await engine.dispose()

    asyncio.run(scenario())


def test_lehai_catalog_is_created_in_gemini_store_and_synced_dynamically() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/balance"):
            return httpx.Response(200, json={"success": True, "balance": 1_000_000})
        return httpx.Response(200, json=product_payload())

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
            lehai_enabled=True,
            lehai_api_key="tgb_test",
        )
        client = LeHaiPremiumClient(
            "https://supplier.test",
            "tgb_test",
            transport=httpx.MockTransport(handler),
        )
        async with sessions() as session:
            legacy_category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(legacy_category)
            await session.flush()
            session.add(
                Product(
                    category_id=legacy_category.id,
                    name_vi="BHF GPT PLUS GMAIL APPLE PAY",
                    name_en="BHF ChatGPT Plus Gmail Apple Pay",
                    price=135_000,
                    allow_quantity=True,
                    max_quantity=100,
                    fulfillment_source="lehai",
                    supplier_product_id="gptupi_kbh12k",
                    supplier_markup=5_000,
                    supplier_price=130_000,
                )
            )
            await session.commit()

        await ensure_lehai_products(sessions, settings)
        await sync_lehai_products(sessions, client)

        async with sessions() as session:
            category = await session.scalar(select(Category).where(Category.name_vi == CATEGORY_VI))
            products = list(
                await session.scalars(
                    select(Product)
                    .where(Product.fulfillment_source == "lehai")
                    .order_by(Product.supplier_product_id)
                )
            )
            assert category is not None
            assert [product.supplier_product_id for product in products] == [
                "cdk_ggpro_18m",
                "cdk_pixel",
                "gptupi_kbh12k",
            ]
            assert [product.price for product in products] == [
                32_000,
                30_000,
                135_000,
            ]
            assert [product.external_stock for product in products] == [37, 40, 7]
            assert all(product.category_id == category.id for product in products[:2])
            chatgpt = await session.scalar(
                select(Category).where(
                    Category.name_vi == "🤖Tài Khoản ChatGPT cá nhân"
                )
            )
            assert chatgpt is not None
            assert products[-1].category_id == chatgpt.id
            legacy_category = await session.scalar(
                select(Category).where(Category.name_vi == "ChatGPT")
            )
            assert legacy_category is None
        await engine.dispose()

    asyncio.run(scenario())


def test_lehai_startup_preserves_products_hidden_by_admin() -> None:
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
            lehai_enabled=True,
            lehai_api_key="tgb_test",
        )
        async with sessions() as session:
            category = Category(name_vi=CATEGORY_VI, name_en=CATEGORY_VI)
            session.add(category)
            await session.flush()
            hidden = Product(
                category_id=category.id,
                name_vi="Link GG Pro Jio 18M",
                name_en="Google Pro Jio 18M Link",
                price=35_000,
                fulfillment_source="lehai",
                supplier_product_id="cdk_ggpro_18m",
                supplier_markup=8_000,
                supplier_price=27_000,
                external_stock=10,
                active=False,
            )
            session.add(hidden)
            await session.commit()
            hidden_id = hidden.id

        await ensure_lehai_products(sessions, settings)

        async with sessions() as session:
            stored = await session.get(Product, hidden_id)
            assert stored is not None
            assert stored.active is False
            assert stored.external_stock == 10
        await engine.dispose()

    asyncio.run(scenario())


class FakeLeHaiSupplier:
    provider = "lehai"

    def __init__(self) -> None:
        self.balance_lock = asyncio.Lock()
        self.idempotency_keys: list[str | None] = []

    async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
        return SupplierSnapshot(
            product_id=product_id,
            name="CDK GG Pro Pixel 1 Năm",
            description="Pixel offer key",
            unit_price=25_000,
            source_stock=20,
            owner_balance=500_000,
        )

    async def fetch_balance(self) -> int:
        return 500_000

    async def buy(
        self,
        product_id: str,
        quantity: int,
        *,
        idempotency_key: str | None = None,
    ) -> SupplierPurchase:
        self.idempotency_keys.append(idempotency_key)
        return SupplierPurchase(
            order_code="LHP-ORDER-SERVICE",
            unit_price=23_000,
            accounts=tuple(f"PIXEL-KEY-{index}" for index in range(quantity)),
            product_id=product_id,
            provider=self.provider,
        )


def test_lehai_wallet_purchase_tracks_dynamic_cost_and_provider() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = FakeLeHaiSupplier()
        async with sessions() as session:
            category = Category(name_vi=CATEGORY_VI, name_en=CATEGORY_VI)
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="CDK GG Pro Pixel 1 Năm",
                name_en="Google Pro Pixel 1 Year CDK",
                price=30_000,
                allow_quantity=True,
                max_quantity=100,
                fulfillment_source="lehai",
                supplier_product_id="cdk_pixel",
                supplier_markup=5_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=60_000)
            session.add_all([product, user])
            await session.commit()

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            2,
            lehai_client=supplier,  # type: ignore[arg-type]
            supplier_idempotency_key="tg-callback-test",
        )

        assert result.ok is True
        assert result.secrets == ["PIXEL-KEY-0", "PIXEL-KEY-1"]
        assert supplier.idempotency_keys == ["tg-callback-test"]
        async with sessions() as session:
            orders = list(await session.scalars(select(Order).order_by(Order.id)))
            purchase_audit = await session.scalar(
                select(SupplierBalanceTransaction).where(
                    SupplierBalanceTransaction.provider == "lehai"
                )
            )
            stored_product = await session.get(Product, product.id)
            assert len(orders) == 2
            assert all(order.cost_amount == 23_000 for order in orders)
            assert stored_product is not None and stored_product.price == 30_000
            assert purchase_audit is not None and purchase_audit.amount == -46_000
        await engine.dispose()

    asyncio.run(scenario())


def test_lehai_direct_qr_purchase_uses_deposit_as_idempotency_key() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = FakeLeHaiSupplier()
        async with sessions() as session:
            category = Category(name_vi=CATEGORY_VI, name_en=CATEGORY_VI)
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Link GG Pro Jio 18M",
                name_en="Google Pro Jio 18M Link",
                price=30_000,
                fulfillment_source="lehai",
                supplier_product_id="cdk_ggpro_18m",
                supplier_markup=5_000,
            )
            user = User(telegram_id=654321, full_name="QR Buyer", balance=0)
            session.add_all([product, user])
            await session.flush()
            session.add(
                Deposit(
                    user_id=user.telegram_id,
                    code="NAP654321ABCD",
                    requested_amount=30_000,
                    payment_kind="direct_purchase",
                    product_id=product.id,
                )
            )
            await session.commit()

        result = await process_sepay_payment(
            sessions,
            {
                "id": 7654321,
                "transferType": "in",
                "transferAmount": 30_000,
                "content": "NAP654321ABCD",
            },
            cipher=cipher,
            lehai_client=supplier,  # type: ignore[arg-type]
        )

        assert result.status == "direct_purchase_completed"
        assert supplier.idempotency_keys == ["qr-NAP654321ABCD"]
        assert [cipher.decrypt(value) for value in result.encrypted_secrets] == [
            "PIXEL-KEY-0"
        ]
        async with sessions() as session:
            order = await session.scalar(select(Order))
            assert order is not None and order.cost_amount == 23_000
            assert order.supplier_order_code == "LHP-ORDER-SERVICE"
        await engine.dispose()

    asyncio.run(scenario())


class ConcurrentLeHaiSupplier(FakeLeHaiSupplier):
    def __init__(self) -> None:
        super().__init__()
        self.balance = 50_000
        self.buy_count = 0
        self.in_flight = 0
        self.max_in_flight = 0

    async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
        return SupplierSnapshot(
            product_id=product_id,
            name="CDK GG Pixel 1Y",
            description="Pixel offer key",
            unit_price=25_000,
            source_stock=2,
            owner_balance=self.balance,
        )

    async def buy(
        self,
        product_id: str,
        quantity: int,
        *,
        idempotency_key: str | None = None,
    ) -> SupplierPurchase:
        self.idempotency_keys.append(idempotency_key)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.01)
            self.buy_count += 1
            self.balance -= 25_000 * quantity
            return SupplierPurchase(
                order_code=f"LHP-CONCURRENT-{self.buy_count}",
                unit_price=25_000,
                accounts=(f"PIXEL-CONCURRENT-{self.buy_count}",),
                product_id=product_id,
                provider=self.provider,
            )
        finally:
            self.in_flight -= 1


def test_simultaneous_lehai_purchases_are_serialized_by_supplier_wallet() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = ConcurrentLeHaiSupplier()
        async with sessions() as session:
            category = Category(name_vi=CATEGORY_VI, name_en=CATEGORY_VI)
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="CDK GG Pixel 1Y",
                name_en="Google Pro Pixel 1 Year CDK",
                price=30_000,
                fulfillment_source="lehai",
                supplier_product_id="cdk_pixel",
                supplier_markup=5_000,
            )
            session.add_all(
                [
                    product,
                    User(telegram_id=10001, full_name="Buyer 1", balance=30_000),
                    User(telegram_id=10002, full_name="Buyer 2", balance=30_000),
                ]
            )
            await session.commit()

        first, second = await asyncio.gather(
            purchase_product(
                sessions,
                10001,
                product.id,
                cipher,
                lehai_client=supplier,  # type: ignore[arg-type]
                supplier_idempotency_key="tg-concurrent-1",
            ),
            purchase_product(
                sessions,
                10002,
                product.id,
                cipher,
                lehai_client=supplier,  # type: ignore[arg-type]
                supplier_idempotency_key="tg-concurrent-2",
            ),
        )

        assert first.ok is True and second.ok is True
        assert supplier.buy_count == 2
        assert supplier.max_in_flight == 1
        assert supplier.balance == 0
        assert supplier.idempotency_keys == ["tg-concurrent-1", "tg-concurrent-2"]
        await engine.dispose()

    asyncio.run(scenario())
