import asyncio
import json

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
    Order,
    Product,
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
        assert request.url.params["key"] == "tgb_test"
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


def test_lehai_purchase_uses_idempotency_and_extracts_delivered_items() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads((await request.aread()).decode())
        assert body == {
            "key": "tgb_test",
            "product_id": "cdk_ggpro_18m",
            "quantity": 2,
            "idempotency_key": "shop-order-123",
        }
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
