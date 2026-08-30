import asyncio
import json

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.canboso_suppliers import CANBOSO_GG18M_ROUTE_ID, CanbosoClient
from app.database import Base
from app.models import Category, Order, Product, User
from app.services import ProductPricing, price_supplier_plan, purchase_product
from app.suppliers import SupplierPurchase, SupplierRoute, SupplierSnapshot
from app.utils import SecretCipher


def test_canboso_converts_usd_catalog_balance_and_purchase_to_vnd() -> None:
    purchase_bodies: list[dict[str, object]] = []
    idempotency_keys: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/products"):
            assert request.url.params["key"] == "tgb_test"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "walletCurrency": "USD",
                    "products": [
                        {
                            "_id": "source-gg18m-id",
                            "product_name": "Link GG Pro Jio 18M",
                            "walletCurrency": "USD",
                            "walletPricing": 0.4,
                            "stats": {"available": 50},
                        }
                    ],
                },
            )
        if request.url.path.endswith("/balance"):
            assert request.url.params["key"] == "tgb_test"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "walletCurrency": "USD",
                    "balanceUsd": 10,
                },
            )
        purchase_bodies.append(json.loads((await request.aread()).decode()))
        idempotency_keys.append(request.headers["Idempotency-Key"])
        return httpx.Response(
            200,
            json={
                "success": True,
                "walletCurrency": "USD",
                "orderCode": "ORDER-CBS-1",
                "amountUsd": 0.8,
                "deliveredAccounts": [
                    {"user": "https://offer.test/one"},
                    {"user": "https://offer.test/two"},
                ],
            },
        )

    async def scenario() -> None:
        client = CanbosoClient(
            "https://supplier.test",
            "tgb_test",
            transport=httpx.MockTransport(handler),
        )
        snapshot = await client.fetch_snapshot(CANBOSO_GG18M_ROUTE_ID)
        purchase = await client.buy(
            CANBOSO_GG18M_ROUTE_ID,
            2,
            idempotency_key="purchase-gg18m-test",
        )

        assert snapshot.unit_price == 11_000
        assert snapshot.source_stock == 50
        assert snapshot.owner_balance == 275_000
        assert snapshot.effective_stock == 25
        assert purchase.unit_price == 11_000
        assert purchase.order_code == "CBS-ORDER-CBS-1"
        assert purchase.accounts == (
            "https://offer.test/one",
            "https://offer.test/two",
        )
        assert purchase_bodies == [
            {
                "key": "tgb_test",
                "product_id": "source-gg18m-id",
                "quantity": 2,
            }
        ]
        assert idempotency_keys == ["purchase-gg18m-test"]
        await client.aclose()

    asyncio.run(scenario())


def test_canboso_supports_current_v21_catalog_and_purchase_response() -> None:
    purchase_bodies: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/products"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "walletCurrency": "USD",
                    "products": [
                        {
                            "productId": "6a492e1100b843ce3de675a7",
                            "name": "GEMINI PRO 18M LINK",
                            "description": "Gemini AI Pro package for 18 months",
                            "productType": "account",
                            "price": {
                                "amount": 0.45,
                                "currency": "USD",
                                "text": "$0.45",
                            },
                            "availability": {"available": 23, "sold": 32306},
                            "promotions": [],
                        }
                    ],
                },
            )
        if request.url.path.endswith("/balance"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "walletCurrency": "USD",
                    "balanceUsd": 27.4,
                },
            )
        purchase_bodies.append(json.loads((await request.aread()).decode()))
        return httpx.Response(
            200,
            json={
                "success": True,
                "lang": "en",
                "order": {
                    "orderCode": "ORDER-CURRENT-1",
                    "status": "completed",
                    "productId": "6a492e1100b843ce3de675a7",
                    "productName": "GEMINI PRO 18M LINK",
                    "productType": "account",
                    "quantity": 1,
                    "bonusQuantity": 0,
                    "finalQuantity": 1,
                },
                "payment": {
                    "amount": 0.45,
                    "currency": "USD",
                    "balance": 26.95,
                },
                "delivery": {
                    "accounts": [{"user": "https://offer.test/current"}],
                },
            },
        )

    async def scenario() -> None:
        client = CanbosoClient(
            "https://supplier.test",
            "tgb_test",
            transport=httpx.MockTransport(handler),
        )
        snapshot = await client.fetch_snapshot(CANBOSO_GG18M_ROUTE_ID)
        purchase = await client.buy(
            CANBOSO_GG18M_ROUTE_ID,
            1,
            idempotency_key="purchase-current-v21",
        )

        assert snapshot.unit_price == 12_375
        assert snapshot.source_stock == 23
        assert snapshot.owner_balance == 753_500
        assert purchase.unit_price == 12_375
        assert purchase.order_code == "CBS-ORDER-CURRENT-1"
        assert purchase.accounts == ("https://offer.test/current",)
        assert purchase_bodies == [
            {
                "key": "tgb_test",
                "product_id": "6a492e1100b843ce3de675a7",
                "quantity": 1,
            }
        ]
        await client.aclose()

    asyncio.run(scenario())


class RoutedSupplier:
    def __init__(self, provider: str, *, price: int, stock: int) -> None:
        self.provider = provider
        self.price = price
        self.stock = stock
        self.balance_lock = asyncio.Lock()
        self.buy_calls: list[tuple[str, int, str | None]] = []

    async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
        return SupplierSnapshot(
            product_id=product_id,
            name="GG Pro 18M",
            description="",
            unit_price=self.price,
            source_stock=self.stock,
            owner_balance=self.price * self.stock,
        )

    async def fetch_balance(self) -> int:
        return self.price * self.stock

    async def buy(
        self,
        product_id: str,
        quantity: int,
        *,
        idempotency_key: str | None = None,
    ) -> SupplierPurchase:
        self.buy_calls.append((product_id, quantity, idempotency_key))
        return SupplierPurchase(
            order_code=f"{self.provider.upper()}-ORDER-1",
            unit_price=self.price,
            accounts=tuple(f"https://offer.test/{self.provider}/{index}" for index in range(quantity)),
            product_id=product_id,
            provider=self.provider,
        )


def test_canboso_multi_source_quote_uses_rounded_shop_price() -> None:
    product = Product(
        price=21_000,
        supplier_markup=9_000,
        price_lock_enabled=False,
    )
    client = RoutedSupplier("canboso", price=12_375, stock=10)
    route = SupplierRoute(
        provider="canboso",
        product_id=CANBOSO_GG18M_ROUTE_ID,
        client=client,  # type: ignore[arg-type]
        snapshot=SupplierSnapshot(
            product_id=CANBOSO_GG18M_ROUTE_ID,
            name="GG Pro 18M",
            description="",
            unit_price=12_375,
            source_stock=10,
            owner_balance=123_750,
        ),
    )
    quote = price_supplier_plan(
        product,
        ((route, 1),),
        ProductPricing(
            original_unit_price=21_000,
            discount_per_unit=0,
            final_unit_price=21_000,
        ),
    )

    assert quote.total_amount == 21_000
    assert quote.allocations[0].original_unit_price == 21_000
    assert quote.allocations[0].final_unit_price == 21_000


def test_gg18m_purchase_prefers_cheaper_canboso_source() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())
        lehai = RoutedSupplier("lehai", price=20_000, stock=10)
        canboso = RoutedSupplier("canboso", price=11_000, stock=10)
        async with sessions() as session:
            category = Category(name_vi="Gemini", name_en="Gemini")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Link GG Pro Jio 18M",
                name_en="Google Pro Jio 18M",
                price=25_000,
                allow_quantity=True,
                max_quantity=100,
                fulfillment_source="lehai",
                supplier_product_id="cdk_ggpro_18m",
                supplier_markup=5_000,
                lehai_api_enabled=True,
                canboso_api_enabled=True,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=100_000)
            session.add_all([product, user])
            await session.commit()

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            1,
            lehai_client=lehai,  # type: ignore[arg-type]
            canboso_client=canboso,  # type: ignore[arg-type]
            supplier_idempotency_key="gg18m-cheapest-source",
        )

        assert result.ok is True
        assert result.total_amount == 16_000
        assert len(canboso.buy_calls) == 1
        assert lehai.buy_calls == []
        async with sessions() as session:
            order = await session.scalar(select(Order))
            assert order is not None
            assert order.supplier_provider == "canboso"
            assert order.cost_amount == 11_000
            assert order.amount == 16_000
        await engine.dispose()

    asyncio.run(scenario())


def test_gg18m_haji_fallback_keeps_canboso_public_price() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())
        lehai = RoutedSupplier("lehai", price=20_000, stock=10)
        canboso = RoutedSupplier("canboso", price=11_000, stock=0)
        haji = RoutedSupplier("haji", price=9_000, stock=10)
        async with sessions() as session:
            category = Category(name_vi="Gemini", name_en="Gemini")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Link GG Pro Jio 18M",
                name_en="Google Pro Jio 18M",
                price=25_000,
                allow_quantity=True,
                max_quantity=100,
                fulfillment_source="lehai",
                supplier_product_id="cdk_ggpro_18m",
                supplier_markup=5_000,
                lehai_api_enabled=True,
                canboso_api_enabled=True,
            )
            user = User(telegram_id=234567, full_name="Buyer", balance=100_000)
            session.add_all([product, user])
            await session.commit()

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            1,
            lehai_client=lehai,  # type: ignore[arg-type]
            canboso_client=canboso,  # type: ignore[arg-type]
            haji_client=haji,  # type: ignore[arg-type]
            supplier_idempotency_key="gg18m-haji-cheapest-source",
        )

        assert result.ok is True
        assert result.total_amount == 16_000
        assert haji.buy_calls[0][:2] == ("link_gemini_18moth", 1)
        assert canboso.buy_calls == []
        assert result.orders[0].supplier_provider == "haji"
        assert result.orders[0].amount == 16_000
        await engine.dispose()

    asyncio.run(scenario())


def test_gg18m_purchase_falls_back_to_lehai_when_canboso_has_no_stock() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())
        lehai = RoutedSupplier("lehai", price=20_000, stock=10)
        canboso = RoutedSupplier("canboso", price=11_000, stock=0)
        async with sessions() as session:
            category = Category(name_vi="Gemini", name_en="Gemini")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Link GG Pro Jio 18M",
                name_en="Google Pro Jio 18M",
                price=25_000,
                allow_quantity=True,
                max_quantity=100,
                fulfillment_source="lehai",
                supplier_product_id="cdk_ggpro_18m",
                supplier_markup=5_000,
                lehai_api_enabled=True,
                canboso_api_enabled=True,
            )
            user = User(telegram_id=654321, full_name="Buyer", balance=100_000)
            session.add_all([product, user])
            await session.commit()

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            1,
            lehai_client=lehai,  # type: ignore[arg-type]
            canboso_client=canboso,  # type: ignore[arg-type]
            supplier_idempotency_key="gg18m-fallback-source",
        )

        assert result.ok is True
        assert result.total_amount == 25_000
        assert canboso.buy_calls == []
        assert len(lehai.buy_calls) == 1
        async with sessions() as session:
            order = await session.scalar(select(Order))
            assert order is not None
            assert order.supplier_provider == "lehai"
            assert order.cost_amount == 20_000
            assert order.amount == 25_000
        await engine.dispose()

    asyncio.run(scenario())


def test_gg18m_purchase_prefers_canboso_when_source_prices_are_equal() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())
        lehai = RoutedSupplier("lehai", price=11_000, stock=10)
        canboso = RoutedSupplier("canboso", price=11_000, stock=10)
        async with sessions() as session:
            category = Category(name_vi="Gemini", name_en="Gemini")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Link GG Pro Jio 18M",
                name_en="Google Pro Jio 18M",
                price=16_000,
                allow_quantity=True,
                max_quantity=100,
                fulfillment_source="lehai",
                supplier_product_id="cdk_ggpro_18m",
                supplier_markup=5_000,
                lehai_api_enabled=True,
                canboso_api_enabled=True,
            )
            user = User(telegram_id=789012, full_name="Buyer", balance=100_000)
            session.add_all([product, user])
            await session.commit()

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            1,
            lehai_client=lehai,  # type: ignore[arg-type]
            canboso_client=canboso,  # type: ignore[arg-type]
            supplier_idempotency_key="gg18m-equal-price",
        )

        assert result.ok is True
        assert len(canboso.buy_calls) == 1
        assert lehai.buy_calls == []
        assert result.orders[0].supplier_provider == "canboso"
        await engine.dispose()

    asyncio.run(scenario())


def test_gg18m_purchase_splits_quantity_across_both_sources() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())
        canboso = RoutedSupplier("canboso", price=11_000, stock=3)
        lehai = RoutedSupplier("lehai", price=20_000, stock=10)
        async with sessions() as session:
            category = Category(name_vi="Gemini", name_en="Gemini")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Link GG Pro Jio 18M",
                name_en="Google Pro Jio 18M",
                price=25_000,
                allow_quantity=True,
                max_quantity=100,
                fulfillment_source="lehai",
                supplier_product_id="cdk_ggpro_18m",
                supplier_markup=5_000,
                lehai_api_enabled=True,
                canboso_api_enabled=True,
            )
            user = User(telegram_id=890123, full_name="Buyer", balance=200_000)
            session.add_all([product, user])
            await session.commit()

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            5,
            lehai_client=lehai,  # type: ignore[arg-type]
            canboso_client=canboso,  # type: ignore[arg-type]
            supplier_idempotency_key="gg18m-split-sources",
        )

        assert result.ok is True
        assert result.total_amount == 98_000
        assert canboso.buy_calls[0][:2] == (CANBOSO_GG18M_ROUTE_ID, 3)
        assert lehai.buy_calls[0][:2] == ("cdk_ggpro_18m", 2)
        assert [order.supplier_provider for order in result.orders].count("canboso") == 3
        assert [order.supplier_provider for order in result.orders].count("lehai") == 2
        await engine.dispose()

    asyncio.run(scenario())
