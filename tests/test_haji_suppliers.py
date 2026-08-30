import asyncio
import json

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.haji_suppliers import HajiClient, ensure_haji_products, haji_product_kind
from app.models import (
    Category,
    Deposit,
    Order,
    PaymentTransaction,
    Preorder,
    Product,
    SupplierBalanceTransaction,
    SupplierPurchaseAttempt,
    User,
)
from app.preorders import _claim_next_preorder, _process_claimed_preorder, create_preorder
from app.services import buy_supplier_product, process_sepay_payment, purchase_product
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
                    "product_id": "apicodex_10m_1day",
                    "name": "API Codex 10M Token 1 ngay (BHF)",
                    "price": 25_000,
                    "currency": "VND",
                    "stock_count": 0,
                    "available": False,
                    "description": "24 gio sau kich hoat",
                },
                {
                    "product_id": "apicodex_50m_1day",
                    "name": "API Codex 50M Token 1 ngay (BHF)",
                    "price": 35_000,
                    "currency": "VND",
                    "stock_count": 2,
                    "available": True,
                    "description": "24 gio sau kich hoat",
                },
                {
                    "product_id": "apicodex_100m_1day",
                    "name": "API Codex 100M Token 1 ngay (BHF)",
                    "price": 55_000,
                    "currency": "VND",
                    "stock_count": 3,
                    "available": True,
                    "description": "24 gio sau kich hoat",
                },
                {
                    "product_id": "other_product",
                    "name": "Other account",
                    "price": 1_000,
                    "stock_count": 100,
                },
            ],
            "total_products": 7,
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
    assert haji_product_kind("apicodex_10m_1day API Codex 10M Token") == "codex"
    assert haji_product_kind("API Codex 50M Token 1 ngay") == "codex"
    assert haji_product_kind("API Codex 100M Token 1 ngay") == "codex"
    assert haji_product_kind("API Codex 500M Token") is None
    assert haji_product_kind("Slot Claude Team Standard") == "claude"


def test_haji_only_exposes_the_requested_claude_sku() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/catalog":
            payload = catalog_payload()
            payload["data"]["products"].extend(
                [
                    {
                        "product_id": "claude_addteam1x25",
                        "name": "Slot Claude Team (Standard) BHF 1 Moth",
                        "price": 400_000,
                        "currency": "VND",
                        "stock_count": 14,
                        "available": True,
                    },
                    {
                        "product_id": "claude_slot_premium",
                        "name": "Claude Slot Premium",
                        "price": 2_100_000,
                        "currency": "VND",
                        "stock_count": 10,
                        "available": True,
                    },
                ]
            )
            return httpx.Response(200, json=payload)
        if request.url.path == "/api/v2/me":
            return httpx.Response(200, json={"ok": True, "data": {"balance": 6_000_000}})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async def scenario() -> None:
        client = HajiClient(
            "https://api.haji.in.net",
            "dl_test_key_123456789",
            transport=httpx.MockTransport(handler),
        )
        products = await client.refresh_catalog(force=True)
        assert {product.product_id for product in products if product.kind == "claude"} == {
            "claude_addteam1x25"
        }
        await client.aclose()

    asyncio.run(scenario())


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
            "codex",
            "codex",
            "codex",
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


def test_haji_manual_claude_purchase_sends_emails_and_polls_until_done() -> None:
    order_requests: list[dict[str, object]] = []
    poll_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        if request.url.path == "/api/v2/catalog":
            payload = catalog_payload()
            payload["data"]["products"].append(
                {
                    "product_id": "claude_addteam1x25",
                    "name": "Slot Claude Team (Standard) BHF 1 Moth",
                    "price": 400_000,
                    "currency": "VND",
                    "stock_count": 14,
                    "available": True,
                    "delivery_mode": "manual_fulfillment",
                    "requires_emails": True,
                }
            )
            return httpx.Response(200, json=payload)
        if request.url.path == "/api/v2/me":
            return httpx.Response(200, json={"ok": True, "data": {"balance": 6_000_000}})
        if request.url.path == "/api/v2/orders" and request.method == "POST":
            order_requests.append(json.loads(request.content))
            assert request.headers["x-idempotency-key"] == "shop-claude-001"
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "order_code": "AP-CLAUDE001",
                        "quantity": 2,
                        "unit_price": 400_000,
                        "total_price": 800_000,
                        "status": "processing",
                    },
                },
            )
        if request.url.path == "/api/v2/orders/AP-CLAUDE001":
            poll_count += 1
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "order_code": "AP-CLAUDE001",
                        "quantity": 2,
                        "unit_price": 400_000,
                        "status": "done",
                        "items": [
                            {"value": "customer-one@example.com", "type": "email"},
                            {"value": "customer-two@example.com", "type": "email"},
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
            manual_poll_seconds=0.2,
            manual_timeout_seconds=1,
        )
        products = await client.refresh_catalog(force=True)
        claude = next(product for product in products if product.product_id == "claude_addteam1x25")
        assert claude.kind == "claude"
        assert claude.delivery_mode == "manual_fulfillment"
        assert claude.requires_emails is True
        purchase = await client.buy(
            claude.product_id,
            2,
            idempotency_key="shop-claude-001",
            emails=("customer-one@example.com", "customer-two@example.com"),
        )
        assert order_requests == [
            {
                "product_id": "claude_addteam1x25",
                "quantity": 2,
                "partner_ref": "shop-claude-001",
                "emails": ["customer-one@example.com", "customer-two@example.com"],
            }
        ]
        assert poll_count == 1
        assert purchase.order_code == "HAJI-AP-CLAUDE001"
        assert purchase.unit_price == 400_000
        assert purchase.accounts == (
            "customer-one@example.com",
            "customer-two@example.com",
        )
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


def test_haji_products_are_imported_with_fixed_codex_sale_prices() -> None:
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
            assert len(rows) == 6
            by_id = {product.supplier_product_id: (product, category) for product, category in rows}
            netflix, netflix_category = by_id["netflix_4k"]
            assert netflix.price == 25_000
            assert netflix.allow_quantity is True and netflix.max_quantity == 100
            assert netflix_category.name_vi == "Netflix"
            assert by_id["gpt_gcash_1m"][1].name_vi == "ChatGPT"
            assert by_id["chatgpt_k12"][1].name_vi == "ChatGPT"
            codex_10m, codex_category = by_id["apicodex_10m_1day"]
            codex_50m = by_id["apicodex_50m_1day"][0]
            codex_100m = by_id["apicodex_100m_1day"][0]
            assert codex_category.name_vi == "API CODEX & CLAUDE"
            assert codex_10m.price == 30_000 and codex_10m.supplier_markup == 5_000
            assert codex_50m.price == 50_000 and codex_50m.supplier_markup == 15_000
            assert codex_100m.price == 70_000 and codex_100m.supplier_markup == 15_000
            assert codex_10m.allow_quantity is False and codex_10m.max_quantity == 1
            assert codex_50m.allow_quantity is False and codex_50m.max_quantity == 1
            assert codex_100m.allow_quantity is False and codex_100m.max_quantity == 1
        await client.aclose()
        await engine.dispose()

    asyncio.run(scenario())


def test_haji_claude_product_is_created_in_codex_category() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/catalog":
            payload = catalog_payload()
            payload["data"]["products"].append(
                {
                    "product_id": "claude_addteam1x25",
                    "name": "Slot Claude Team (Standard) BHF 1 Moth",
                    "price": 400_000,
                    "currency": "VND",
                    "stock_count": 14,
                    "available": True,
                    "delivery_mode": "manual_fulfillment",
                    "requires_emails": True,
                }
            )
            return httpx.Response(200, json=payload)
        if request.url.path == "/api/v2/me":
            return httpx.Response(200, json={"ok": True, "data": {"balance": 6_000_000}})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

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
        await ensure_haji_products(sessions, client, markup=5_000)
        async with sessions() as session:
            product, category = (
                await session.execute(
                    select(Product, Category)
                    .join(Category, Category.id == Product.category_id)
                    .where(Product.supplier_product_id == "claude_addteam1x25")
                )
            ).one()
            assert product.name_vi == "Claude Team Standard 1 tháng"
            assert product.price == 405_000
            assert product.product_type == "service"
            assert product.allow_quantity is True and product.max_quantity == 100
            assert product.external_stock == 0
            assert category.name_vi == "API CODEX & CLAUDE"
        await client.aclose()
        await engine.dispose()

    asyncio.run(scenario())


def test_haji_catalog_sync_preserves_admin_product_edits() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/catalog":
            return httpx.Response(200, json=catalog_payload())
        if request.url.path == "/api/v2/me":
            return httpx.Response(200, json={"ok": True, "data": {"balance": 100_000}})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            category = Category(name_vi="Gian hang tuy chinh", name_en="Custom category")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Ten admin da sua",
                name_en="Admin custom name",
                description_vi="Mo ta admin da sua",
                description_en="Admin custom description",
                price=27_000,
                product_type="account",
                allow_quantity=False,
                max_quantity=3,
                fulfillment_source="haji",
                supplier_product_id="netflix_4k",
                supplier_markup=7_000,
                supplier_price=20_000,
                external_stock=4,
                active=False,
            )
            session.add(product)
            await session.commit()
            category_id = category.id
            product_id = product.id

        client = HajiClient(
            "https://api.haji.in.net",
            "dl_test_key_123456789",
            transport=httpx.MockTransport(handler),
        )
        await ensure_haji_products(sessions, client, markup=5_000)

        async with sessions() as session:
            product = await session.get(Product, product_id)
            assert product is not None
            assert product.category_id == category_id
            assert product.name_vi == "Ten admin da sua"
            assert product.name_en == "Admin custom name"
            assert product.description_vi == "Mo ta admin da sua"
            assert product.description_en == "Admin custom description"
            assert product.price == 27_000
            assert product.supplier_markup == 7_000
            assert product.allow_quantity is False
            assert product.max_quantity == 3
            assert product.active is False

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


class CodexHajiSupplier:
    provider = "haji"

    def __init__(self, *, unit_price: int, stock: int, balance: int) -> None:
        self.unit_price = unit_price
        self.stock = stock
        self.balance = balance
        self.balance_lock = asyncio.Lock()
        self.idempotency_keys: list[str | None] = []
        self.buy_count = 0
        self.in_flight = 0
        self.max_in_flight = 0

    async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
        return SupplierSnapshot(
            product_id=product_id,
            name="API Codex 50M Token 1 ngay",
            description="24 gio sau kich hoat",
            unit_price=self.unit_price,
            source_stock=self.stock,
            owner_balance=self.balance,
        )

    async def fetch_balance(self) -> int:
        return self.balance

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
            if quantity > self.stock:
                raise RuntimeError("test attempted to oversell Codex stock")
            self.stock -= quantity
            self.balance -= self.unit_price * quantity
            self.buy_count += 1
            return SupplierPurchase(
                order_code=f"HAJI-CODEX-{self.buy_count}",
                unit_price=self.unit_price,
                accounts=tuple(
                    f"sk-codex-test-{self.buy_count}-{index}"
                    for index in range(quantity)
                ),
                product_id=product_id,
                provider=self.provider,
            )
        finally:
            self.in_flight -= 1


async def seed_codex_product(
    sessions: async_sessionmaker,
    *,
    supplier_product_id: str = "apicodex_50m_1day",
    sale_price: int = 50_000,
    supplier_price: int = 35_000,
    stock: int = 1,
    users: tuple[tuple[int, int], ...] = (),
) -> int:
    async with sessions() as session:
        category = Category(name_vi="API CODEX", name_en="CODEX API")
        session.add(category)
        await session.flush()
        product = Product(
            category_id=category.id,
            name_vi="API Codex 50M Token · 24 giờ",
            name_en="Codex API 50M Tokens · 24 hours",
            price=sale_price,
            allow_quantity=False,
            max_quantity=1,
            fulfillment_source="haji",
            supplier_product_id=supplier_product_id,
            supplier_markup=sale_price - supplier_price,
            supplier_price=supplier_price,
            external_stock=stock,
        )
        session.add(product)
        session.add_all(
            User(telegram_id=user_id, full_name=f"Buyer {user_id}", balance=balance)
            for user_id, balance in users
        )
        await session.commit()
        return product.id


def test_codex_direct_qr_purchase_delivers_key_and_records_financial_trace() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = CodexHajiSupplier(unit_price=35_000, stock=1, balance=335_000)
        product_id = await seed_codex_product(
            sessions,
            users=((50_001, 0),),
        )
        async with sessions() as session:
            session.add(
                Deposit(
                    user_id=50_001,
                    code="NAP50001C001",
                    requested_amount=50_000,
                    payment_kind="direct_purchase",
                    product_id=product_id,
                    quantity=1,
                )
            )
            await session.commit()

        result = await process_sepay_payment(
            sessions,
            {
                "id": "SEPAY-CODEX-001",
                "transferType": "in",
                "transferAmount": 50_000,
                "content": "NAP50001C001",
            },
            cipher=cipher,
            haji_client=supplier,  # type: ignore[arg-type]
        )

        assert result.status == "direct_purchase_completed"
        assert result.supplier_product_id == "apicodex_50m_1day"
        assert supplier.idempotency_keys == ["qr-NAP50001C001"]
        assert [cipher.decrypt(value) for value in result.encrypted_secrets] == [
            "sk-codex-test-1-0"
        ]
        async with sessions() as session:
            order = await session.scalar(select(Order))
            attempt = await session.scalar(select(SupplierPurchaseAttempt))
            audit = await session.scalar(select(SupplierBalanceTransaction))
            payment = await session.scalar(select(PaymentTransaction))
            user = await session.get(User, 50_001)
            assert order is not None
            assert order.amount == 50_000 and order.cost_amount == 35_000
            assert order.supplier_provider == "haji"
            assert order.supplier_order_code == "HAJI-CODEX-1"
            assert attempt is not None and attempt.status == "succeeded"
            assert attempt.request_key == "qr-NAP50001C001"
            assert attempt.supplier_order_code == "HAJI-CODEX-1"
            assert audit is not None and audit.amount == -35_000
            assert payment is not None and payment.credit_status == "credited"
            assert user is not None and user.balance == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_simultaneous_codex_wallet_purchases_do_not_oversell_one_key() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = CodexHajiSupplier(unit_price=35_000, stock=1, balance=35_000)
        product_id = await seed_codex_product(
            sessions,
            users=((60_001, 50_000), (60_002, 50_000)),
        )

        results = await asyncio.gather(
            purchase_product(
                sessions,
                60_001,
                product_id,
                cipher,
                haji_client=supplier,  # type: ignore[arg-type]
                supplier_idempotency_key="codex-concurrent-1",
            ),
            purchase_product(
                sessions,
                60_002,
                product_id,
                cipher,
                haji_client=supplier,  # type: ignore[arg-type]
                supplier_idempotency_key="codex-concurrent-2",
            ),
        )

        assert [result.ok for result in results].count(True) == 1
        assert [result.message for result in results].count("out_of_stock") == 1
        assert supplier.buy_count == 1
        assert supplier.max_in_flight == 1
        assert supplier.balance == 0 and supplier.stock == 0
        assert len(supplier.idempotency_keys) == 1
        async with sessions() as session:
            assert int(await session.scalar(select(func.count(Order.id))) or 0) == 1
            assert int(
                await session.scalar(select(func.count(SupplierPurchaseAttempt.id))) or 0
            ) == 1
            balances = list(await session.scalars(select(User.balance).order_by(User.telegram_id)))
            assert balances == [0, 50_000]
        await engine.dispose()

    asyncio.run(scenario())


def test_codex_preorder_charges_five_percent_and_fulfills_with_stable_key() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = CodexHajiSupplier(unit_price=25_000, stock=0, balance=100_000)
        product_id = await seed_codex_product(
            sessions,
            supplier_product_id="apicodex_10m_1day",
            sale_price=30_000,
            supplier_price=25_000,
            stock=0,
            users=((70_001, 50_000),),
        )
        async with sessions() as session:
            preorder = await create_preorder(
                session,
                70_001,
                product_id,
                1,
                expected_base_unit_price=30_000,
                max_active_per_user=5,
            )
            await session.commit()
            preorder_id = preorder.id
            assert preorder.total_amount == 31_500

        supplier.stock = 1
        async with sessions() as session:
            product = await session.get(Product, product_id)
            assert product is not None
            product.external_stock = 1
            await session.commit()

        claimed = await _claim_next_preorder(sessions)
        assert claimed is not None and claimed.id == preorder_id
        await _process_claimed_preorder(
            sessions,
            claimed,
            cipher,
            None,
            None,
            None,
            None,
            supplier,  # type: ignore[arg-type]
            0,
        )

        async with sessions() as session:
            preorder = await session.get(Preorder, preorder_id)
            order = await session.scalar(select(Order).where(Order.preorder_id == preorder_id))
            attempt = await session.scalar(select(SupplierPurchaseAttempt))
            user = await session.get(User, 70_001)
            assert preorder is not None and preorder.status == "completed"
            assert preorder.completed_order_code == order.shop_order_code
            assert order is not None
            assert order.amount == 31_500 and order.cost_amount == 25_000
            assert order.sales_channel == "preorder"
            assert order.supplier_provider == "haji"
            assert attempt is not None and attempt.request_key == f"preorder-{preorder_id}"
            assert user is not None and user.balance == 18_500
        assert supplier.idempotency_keys == [f"preorder-{preorder_id}"]
        await engine.dispose()

    asyncio.run(scenario())
