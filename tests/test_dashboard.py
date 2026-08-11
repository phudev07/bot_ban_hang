import asyncio
import re
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import create_api
from app.config import Settings
from app.dashboard import balance_adjustment_notification, group_order_rows
from app.dashboard_security import hash_dashboard_password
from app.database import Base
from app.models import (
    BalanceAdjustment,
    BroadcastLog,
    ApiClient,
    ApiRequestAudit,
    Category,
    Deposit,
    DiscountCode,
    InventoryDuplicateAlert,
    InventoryItem,
    Order,
    PaymentTransaction,
    Product,
    ProductPriceAlert,
    ProductStockAlert,
    QuantityDiscount,
    SellerPrice,
    SellerPriceAudit,
    SmsRental,
    SupplierBalanceState,
    SupplierBalanceTransaction,
    SupplierPurchaseAttempt,
    SupplierRecoveryRequest,
    User,
    WalletTransaction,
)
from app.price_alerts import apply_supplier_price
from app.services import active_products, purchase_product
from app.suppliers import SupplierError, SupplierPurchase, SupplierSnapshot
from app.utils import SecretCipher


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def send_message(self, *_args, **_kwargs) -> None:
        self.messages.append((_args, _kwargs))


class DashboardSupplier:
    def __init__(self, provider: str, *, price: int, stock: int, balance: int) -> None:
        self.provider = provider
        self.price = price
        self.stock = stock
        self.balance = balance
        self.fetch_product_ids: list[str] = []

    async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
        self.fetch_product_ids.append(product_id)
        return SupplierSnapshot(
            product_id=product_id,
            name="GPT Plus",
            description="",
            unit_price=self.price,
            source_stock=self.stock,
            owner_balance=self.balance,
        )


class DashboardBuyingSupplier:
    provider = "sumistore"

    def __init__(self, *, price: int, stock: int, balance: int) -> None:
        self.price = price
        self.stock = stock
        self.balance = balance
        self.balance_lock = asyncio.Lock()
        self.buy_calls: list[tuple[str, int]] = []
        self.order_number = 0

    async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
        return SupplierSnapshot(
            product_id=product_id,
            name="API warranty product",
            description="",
            unit_price=self.price,
            source_stock=self.stock,
            owner_balance=self.balance,
        )

    async def buy(self, product_id: str, quantity: int) -> SupplierPurchase:
        if quantity > self.stock:
            raise SupplierError("INSUFFICIENT_STOCK")
        if quantity * self.price > self.balance:
            raise SupplierError("INSUFFICIENT_BALANCE")
        self.buy_calls.append((product_id, quantity))
        self.order_number += 1
        self.stock -= quantity
        self.balance -= quantity * self.price
        return SupplierPurchase(
            order_code=f"API-WARRANTY-{self.order_number}",
            unit_price=self.price,
            accounts=tuple(
                f"api-warranty-{self.order_number}-{index}@example.com|password-{index}"
                for index in range(1, quantity + 1)
            ),
            product_id=product_id,
            provider=self.provider,
        )


class AmbiguousDashboardSupplier(DashboardBuyingSupplier):
    async def buy(self, product_id: str, quantity: int) -> SupplierPurchase:
        self.buy_calls.append((product_id, quantity))
        raise SupplierError("SUPPLIER_UNAVAILABLE")

    async def recover_recent_purchase(
        self,
        _product_id: str,
        _quantity: int,
        *,
        started_at: datetime,
        known_order_codes: set[str],
    ) -> None:
        assert started_at.tzinfo is not None
        assert isinstance(known_order_codes, set)
        return None


def test_admin_can_manage_dynamic_seller_profit_by_user_and_product(tmp_path) -> None:
    async def initialize():
        database_path = (tmp_path / "dashboard-seller-prices.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            seller = User(
                telegram_id=73001,
                full_name="Seller Test",
                username="seller_test",
                has_started=True,
            )
            session.add_all([category, seller])
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus Seller",
                name_en="GPT Plus Seller",
                price=40_000,
                fulfillment_source="local",
            )
            session.add(product)
            await session.flush()
            session.add(
                InventoryItem(
                    product_id=product.id,
                    encrypted_secret="seller-stock",
                    cost_amount=30_000,
                )
            )
            await session.commit()
        return engine, sessions, product.id, seller.telegram_id

    engine, sessions, product_id, seller_id = asyncio.run(initialize())
    encryption_key = Fernet.generate_key().decode()
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=encryption_key,
        dashboard_enabled=True,
        dashboard_username="admin",
        dashboard_password_hash=hash_dashboard_password("dashboard-password"),
        dashboard_session_secret="session-secret-long-enough-for-tests",
    )
    app = create_api(
        settings,
        sessions,
        FakeBot(),  # type: ignore[arg-type]
        SecretCipher(encryption_key),
    )

    with TestClient(app, base_url="https://testserver") as client:
        client.post(
            "/admin/login",
            data={"username": "admin", "password": "dashboard-password"},
        )
        page = client.get("/admin/seller-prices")
        assert page.status_code == 200
        assert "Giá riêng tự chạy theo giá vốn" in page.text
        assert "GPT Plus Seller" in page.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
        created = client.post(
            "/admin/seller-prices",
            data={
                "csrf": csrf,
                "seller_user": "@seller_test",
                "product_id": str(product_id),
                "profit_per_unit": "5.000",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303
        configured = client.get("/admin/seller-prices")
        assert "Seller Test" in configured.text
        assert "+5.000đ/1" in configured.text
        assert "35.000đ" in configured.text

        rejected = client.post(
            "/admin/seller-prices",
            data={
                "csrf": csrf,
                "seller_user": str(seller_id),
                "product_id": str(product_id),
                "profit_per_unit": "10.000",
            },
            follow_redirects=False,
        )
        assert rejected.status_code == 303
        rejected_page = client.get("/admin/seller-prices")
        assert "phải thấp hơn giá bán thường" in rejected_page.text

    async def verify() -> None:
        async with sessions() as session:
            rule = await session.scalar(select(SellerPrice))
            audits = list(await session.scalars(select(SellerPriceAudit)))
            assert rule is not None
            assert rule.user_id == seller_id
            assert rule.product_id == product_id
            assert rule.profit_per_unit == 5_000
            assert len(audits) == 1

    asyncio.run(verify())
    asyncio.run(engine.dispose())


def test_product_filters_and_inventory_import_only_allow_visible_products(
    tmp_path,
) -> None:
    async def initialize():
        database_path = (tmp_path / "dashboard-product-filters.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            category = Category(name_vi="Tài khoản", name_en="Accounts")
            session.add(category)
            await session.flush()
            visible_stocked = Product(
                category_id=category.id,
                name_vi="Sản phẩm hiện còn hàng",
                name_en="Visible in stock",
                price=40_000,
                fulfillment_source="local",
                active=True,
            )
            visible_empty = Product(
                category_id=category.id,
                name_vi="Sản phẩm hiện hết hàng",
                name_en="Visible out of stock",
                price=40_000,
                fulfillment_source="local",
                active=True,
            )
            hidden_stocked = Product(
                category_id=category.id,
                name_vi="Sản phẩm ẩn còn hàng",
                name_en="Hidden in stock",
                price=40_000,
                fulfillment_source="local",
                active=False,
            )
            session.add_all([visible_stocked, visible_empty, hidden_stocked])
            await session.flush()
            session.add_all(
                [
                    InventoryItem(
                        product_id=visible_stocked.id,
                        encrypted_secret="visible-stock",
                        cost_amount=30_000,
                    ),
                    InventoryItem(
                        product_id=hidden_stocked.id,
                        encrypted_secret="hidden-stock",
                        cost_amount=30_000,
                    ),
                ]
            )
            await session.commit()
        return engine, sessions, hidden_stocked.id

    engine, sessions, hidden_product_id = asyncio.run(initialize())
    encryption_key = Fernet.generate_key().decode()
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=encryption_key,
        dashboard_enabled=True,
        dashboard_username="admin",
        dashboard_password_hash=hash_dashboard_password("dashboard-password"),
        dashboard_session_secret="session-secret-long-enough-for-tests",
    )
    app = create_api(
        settings,
        sessions,
        FakeBot(),  # type: ignore[arg-type]
        SecretCipher(encryption_key),
    )

    with TestClient(app, base_url="https://testserver") as client:
        client.post(
            "/admin/login",
            data={"username": "admin", "password": "dashboard-password"},
        )
        all_products = client.get("/admin/products")
        assert "Sản phẩm hiện còn hàng" in all_products.text
        assert "Sản phẩm hiện hết hàng" in all_products.text
        assert "Sản phẩm ẩn còn hàng" in all_products.text
        assert re.search(r">Đang hiển thị <span>2</span>", all_products.text)
        assert re.search(r">Đang ẩn <span>1</span>", all_products.text)
        assert re.search(r">Còn hàng <span>2</span>", all_products.text)
        assert re.search(r">Hết hàng <span>1</span>", all_products.text)

        visible = client.get("/admin/products?status=visible")
        assert "Sản phẩm hiện còn hàng" in visible.text
        assert "Sản phẩm hiện hết hàng" in visible.text
        assert "Sản phẩm ẩn còn hàng" not in visible.text

        hidden = client.get("/admin/products?status=hidden")
        assert "Sản phẩm ẩn còn hàng" in hidden.text
        assert "Sản phẩm hiện còn hàng" not in hidden.text

        in_stock = client.get("/admin/products?status=in_stock")
        assert "Sản phẩm hiện còn hàng" in in_stock.text
        assert "Sản phẩm ẩn còn hàng" in in_stock.text
        assert "Sản phẩm hiện hết hàng" not in in_stock.text

        out_of_stock = client.get("/admin/products?status=out_of_stock")
        assert "Sản phẩm hiện hết hàng" in out_of_stock.text
        assert "Sản phẩm hiện còn hàng" not in out_of_stock.text

        inventory_page = client.get("/admin/inventory")
        import_select = re.search(
            r'<select name="product_id" required>(.*?)</select>',
            inventory_page.text,
            flags=re.DOTALL,
        )
        assert import_select is not None
        assert "Sản phẩm hiện còn hàng" in import_select.group(1)
        assert "Sản phẩm hiện hết hàng" in import_select.group(1)
        assert "Sản phẩm ẩn còn hàng" not in import_select.group(1)
        csrf = re.search(
            r'name="csrf" value="([^"]+)"', inventory_page.text
        ).group(1)
        rejected = client.post(
            "/admin/inventory",
            data={
                "csrf": csrf,
                "product_id": str(hidden_product_id),
                "items": "hidden-import@example.com|password",
                "cost_amount": "30.000",
            },
            follow_redirects=False,
        )
        assert rejected.status_code == 303
        rejected_page = client.get("/admin/inventory")
        assert "Sản phẩm đang ẩn" in rejected_page.text

    async def verify_hidden_stock_unchanged() -> None:
        async with sessions() as session:
            hidden_stock = int(
                await session.scalar(
                    select(func.count(InventoryItem.id)).where(
                        InventoryItem.product_id == hidden_product_id
                    )
                )
                or 0
            )
            assert hidden_stock == 1

    asyncio.run(verify_hidden_stock_unchanged())
    asyncio.run(engine.dispose())


def test_archived_catalog_items_disappear_but_keep_financial_history(tmp_path) -> None:
    engine, sessions, ids = None, None, None

    async def initialize():
        nonlocal engine, sessions, ids
        database_path = (tmp_path / "dashboard-archive-product.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            category = Category(name_vi="Claude Max", name_en="Claude Max")
            user = User(telegram_id=123456, full_name="Refunded buyer")
            session.add_all([category, user])
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Claude Max 20 KBH",
                name_en="Claude Max 20 KBH",
                price=49_000,
                fulfillment_source="local",
            )
            session.add(product)
            await session.flush()
            item = InventoryItem(
                product_id=product.id,
                encrypted_secret="historical-delivery",
                cost_amount=39_000,
                status="sold",
                sold_at=datetime.now(UTC),
            )
            session.add(item)
            await session.flush()
            session.add_all(
                [
                    Order(
                        user_id=user.telegram_id,
                        product_id=product.id,
                        inventory_item_id=item.id,
                        amount=49_000,
                        cost_amount=39_000,
                        status="completed",
                        delivered_at=datetime.now(UTC),
                    ),
                    Deposit(
                        user_id=user.telegram_id,
                        code="NAPCLAUDETEST",
                        requested_amount=49_000,
                        paid_amount=49_000,
                        payment_kind="direct_purchase",
                        product_id=product.id,
                        status="paid",
                    ),
                ]
            )
            await session.commit()
            ids = (category.id, product.id)

    asyncio.run(initialize())
    assert engine is not None and sessions is not None and ids is not None
    category_id, product_id = ids
    encryption_key = Fernet.generate_key().decode()
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=encryption_key,
        dashboard_enabled=True,
        dashboard_username="admin",
        dashboard_password_hash=hash_dashboard_password("dashboard-password"),
        dashboard_session_secret="session-secret-long-enough-for-tests",
    )
    bot = FakeBot()
    app = create_api(settings, sessions, bot, SecretCipher(encryption_key))  # type: ignore[arg-type]

    with TestClient(app, base_url="https://testserver") as client:
        client.post(
            "/admin/login",
            data={"username": "admin", "password": "dashboard-password"},
        )
        products_page = client.get("/admin/products")
        csrf = re.search(r'name="csrf" value="([^"]+)"', products_page.text).group(1)  # type: ignore[union-attr]
        assert "Claude Max 20 KBH" in products_page.text

        archived_product = client.post(
            f"/admin/products/{product_id}/delete",
            data={"csrf": csrf},
            follow_redirects=False,
        )
        assert archived_product.status_code == 303
        assert "Claude Max 20 KBH" not in client.get("/admin/products").text
        assert "Claude Max 20 KBH" in client.get("/admin/orders").text

        archived_category = client.post(
            f"/admin/categories/{category_id}/delete",
            data={"csrf": csrf},
            follow_redirects=False,
        )
        assert archived_category.status_code == 303
        assert "Claude Max" not in client.get("/admin/categories").text

    async def verify_history() -> None:
        async with sessions() as session:
            category = await session.get(Category, category_id)
            product = await session.get(Product, product_id)
            assert category is not None and category.archived_at is not None
            assert product is not None and product.archived_at is not None
            assert product.active is False
            assert await active_products(session) == []
            assert int(await session.scalar(select(func.count(Order.id))) or 0) == 1
            assert int(await session.scalar(select(func.count(Deposit.id))) or 0) == 1
            assert int(await session.scalar(select(func.count(InventoryItem.id))) or 0) == 1

    asyncio.run(verify_history())
    asyncio.run(engine.dispose())


def test_grouped_orders_show_mixed_supplier_and_manual_inventory_sources() -> None:
    now = datetime.now(UTC)
    product = Product(id=1, category_id=1, name_vi="GPT Plus", name_en="GPT Plus")
    user = User(telegram_id=1, full_name="Buyer")
    rows = [
        (
            Order(
                id=1,
                user_id=1,
                product_id=1,
                inventory_item_id=1,
                amount=30_000,
                cost_amount=25_000,
                discount_amount=0,
                batch_code="BMIXED",
                supplier_provider="sumistore",
                status="completed",
                sales_channel="telegram",
                created_at=now,
                delivered_at=now,
            ),
            product,
            user,
        ),
        (
            Order(
                id=2,
                user_id=1,
                product_id=1,
                inventory_item_id=2,
                amount=30_000,
                cost_amount=25_000,
                discount_amount=0,
                batch_code="BMIXED",
                supplier_provider="lehai",
                status="completed",
                sales_channel="telegram",
                created_at=now,
                delivered_at=now,
            ),
            product,
            user,
        ),
        (
            Order(
                id=3,
                user_id=1,
                product_id=1,
                inventory_item_id=3,
                amount=30_000,
                cost_amount=20_000,
                discount_amount=0,
                batch_code="BLOCAL",
                status="completed",
                sales_channel="telegram",
                created_at=now,
                delivered_at=now,
            ),
            product,
            user,
        ),
    ]

    groups = {group["shop_order_code"]: group for group in group_order_rows(rows)}

    assert groups["BMIXED"]["supplier_source_label"] == "Sumi + Lê Hải"
    assert groups["BMIXED"]["supplier_source_external"] is True
    assert groups["BLOCAL"]["supplier_source_label"] == "Kho bot"
    assert groups["BLOCAL"]["supplier_source_external"] is False


def test_balance_adjustment_notification_shows_sign_reason_and_new_balance() -> None:
    added = balance_adjustment_notification(
        amount=10_000,
        balance=60_000,
        reason="Hoàn tiền <đơn lỗi>",
        language="vi",
    )
    deducted = balance_adjustment_notification(
        amount=-5_000,
        balance=55_000,
        reason="Điều chỉnh nhầm tiền",
        language="vi",
    )

    assert "<b>+10.000đ</b>" in added
    assert "Hoàn tiền &lt;đơn lỗi&gt;" in added
    assert "<b>60.000đ</b>" in added
    assert "<b>-5.000đ</b>" in deducted


def test_canboso_audit_shows_vnd_and_usd_for_totals_and_transactions(tmp_path) -> None:
    async def setup_database():
        database_path = (tmp_path / "dashboard-canboso-audit.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(UTC)
        async with sessions() as session:
            session.add(
                SupplierBalanceState(
                    provider="canboso",
                    last_balance=11_000,
                    checked_at=now,
                )
            )
            session.add_all(
                [
                    SupplierBalanceTransaction(
                        provider="canboso",
                        kind="credit",
                        amount=27_500,
                        balance_before=0,
                        balance_after=27_500,
                        created_at=now,
                    ),
                    SupplierBalanceTransaction(
                        provider="canboso",
                        kind="purchase",
                        amount=-11_000,
                        balance_before=27_500,
                        balance_after=16_500,
                        created_at=now,
                    ),
                    SupplierBalanceTransaction(
                        provider="canboso",
                        kind="suspicious",
                        amount=-5_500,
                        balance_before=16_500,
                        balance_after=11_000,
                        created_at=now,
                    ),
                ]
            )
            await session.commit()
        return engine, sessions

    engine, sessions = asyncio.run(setup_database())
    encryption_key = Fernet.generate_key().decode()
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=encryption_key,
        dashboard_enabled=True,
        dashboard_username="admin",
        dashboard_password_hash=hash_dashboard_password("dashboard-password"),
        dashboard_session_secret="session-secret-long-enough-for-tests",
        canboso_usd_to_vnd=27_500,
    )
    app = create_api(
        settings,
        sessions,
        FakeBot(),
        SecretCipher(encryption_key),
    )  # type: ignore[arg-type]

    with TestClient(app, base_url="https://testserver") as client:
        client.post(
            "/admin/login",
            data={"username": "admin", "password": "dashboard-password"},
        )
        page = client.get("/admin/supplier-audit?provider=canboso")

        assert page.status_code == 200
        assert "11.000đ" in page.text
        assert "$0.40" in page.text
        assert "+27.500đ" in page.text
        assert "+$1.00" in page.text
        assert "-11.000đ" in page.text
        assert "-$0.40" in page.text
        assert "16.500đ → 11.000đ" in page.text
        assert "$0.60 → $0.40" in page.text

    asyncio.run(engine.dispose())


def test_admin_products_shows_selected_gpt_supplier_and_each_stock(tmp_path) -> None:
    async def setup_database():
        database_path = (tmp_path / "dashboard-gpt-routes.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            session.add(
                Product(
                    category_id=category.id,
                    name_vi="GPT Plus",
                    name_en="GPT Plus",
                    price=35_000,
                    fulfillment_source="sumistore",
                    supplier_product_id="SP-GEF55PBV",
                    supplier_markup=5_000,
                    supplier_price=30_000,
                    external_stock=2,
                    supplier_available_stock=2,
                )
            )
            await session.commit()
        return engine, sessions

    engine, sessions = asyncio.run(setup_database())
    encryption_key = Fernet.generate_key().decode()
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=encryption_key,
        dashboard_enabled=True,
        dashboard_username="admin",
        dashboard_password_hash=hash_dashboard_password("dashboard-password"),
        dashboard_session_secret="session-secret-long-enough-for-tests",
    )
    sumi = DashboardSupplier("sumistore", price=30_000, stock=2, balance=60_000)
    lehai = DashboardSupplier("lehai", price=25_000, stock=7, balance=175_000)
    app = create_api(
        settings,
        sessions,
        FakeBot(),  # type: ignore[arg-type]
        SecretCipher(encryption_key),
        supplier_client=sumi,  # type: ignore[arg-type]
        lehai_client=lehai,  # type: ignore[arg-type]
    )

    with TestClient(app, base_url="https://testserver") as client:
        client.post(
            "/admin/login",
            data={"username": "admin", "password": "dashboard-password"},
        )
        page = client.get("/admin/products")
        assert page.status_code == 200
        assert "API đang đấu: Lê Hải" in page.text
        assert re.search(r"Sumi:\s*<strong>2</strong>", page.text)
        assert re.search(r"Lê Hải:\s*<strong>7</strong>", page.text)
        assert re.search(r'<span class="stock[^\"]*">9</span>', page.text)

    asyncio.run(engine.dispose())


def test_admin_can_disable_each_gpt_plus_api_source(tmp_path) -> None:
    async def setup_database():
        database_path = (tmp_path / "dashboard-gpt-api-switches.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus",
                name_en="GPT Plus",
                price=30_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_markup=5_000,
                supplier_price=25_000,
                external_stock=10,
                supplier_available_stock=9,
                supplier_available_stock_initialized=True,
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
            await session.commit()
        return engine, sessions, category.id, product.id

    engine, sessions, category_id, product_id = asyncio.run(setup_database())
    encryption_key = Fernet.generate_key().decode()
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=encryption_key,
        dashboard_enabled=True,
        dashboard_username="admin",
        dashboard_password_hash=hash_dashboard_password("dashboard-password"),
        dashboard_session_secret="session-secret-long-enough-for-tests",
    )
    sumi = DashboardSupplier("sumistore", price=25_000, stock=2, balance=50_000)
    lehai = DashboardSupplier("lehai", price=20_000, stock=7, balance=140_000)
    app = create_api(
        settings,
        sessions,
        FakeBot(),  # type: ignore[arg-type]
        SecretCipher(encryption_key),
        supplier_client=sumi,  # type: ignore[arg-type]
        lehai_client=lehai,  # type: ignore[arg-type]
    )

    def product_form(csrf: str, **overrides: str) -> dict[str, str]:
        data = {
            "csrf": csrf,
            "category_id": str(category_id),
            "name_vi": "GPT Plus",
            "name_en": "GPT Plus",
            "price": "30.000",
            "description_vi": "",
            "description_en": "",
            "product_type": "account",
            "fulfillment_source": "sumistore",
            "supplier_product_id": "SP-GEF55PBV",
            "supplier_markup": "5.000",
            "api_source_controls_present": "1",
            "allow_quantity": "1",
            "max_quantity": "10",
            "active": "1",
        }
        data.update(overrides)
        return data

    with TestClient(app, base_url="https://testserver") as client:
        client.post(
            "/admin/login",
            data={"username": "admin", "password": "dashboard-password"},
        )
        edit_page = client.get(f"/admin/products/{product_id}")
        csrf = re.search(r'name="csrf" value="([^"]+)"', edit_page.text).group(1)
        assert 'name="sumistore_api_enabled"' in edit_page.text
        assert 'name="lehai_api_enabled"' in edit_page.text
        assert "Kho bot đang có 1 tài khoản và luôn được bán trước" in edit_page.text
        assert re.search(r'<option value="local"[^>]*disabled', edit_page.text)

        accidental_local = client.post(
            f"/admin/products/{product_id}",
            data=product_form(
                csrf,
                fulfillment_source="local",
                supplier_product_id="",
                sumistore_api_enabled="1",
                lehai_api_enabled="1",
            ),
            follow_redirects=False,
        )
        assert accidental_local.status_code == 303

        async def verify_hybrid_route_preserved() -> None:
            async with sessions() as session:
                product = await session.get(Product, product_id)
                assert product is not None
                assert product.fulfillment_source == "sumistore"
                assert product.supplier_product_id == "SP-GEF55PBV"
                assert product.sumistore_api_enabled is True
                assert product.lehai_api_enabled is True

        asyncio.run(verify_hybrid_route_preserved())

        only_sumi = client.post(
            f"/admin/products/{product_id}",
            data=product_form(csrf, sumistore_api_enabled="1"),
            follow_redirects=False,
        )
        assert only_sumi.status_code == 303

        async def verify_only_sumi() -> None:
            async with sessions() as session:
                product = await session.get(Product, product_id)
                assert product is not None
                assert product.sumistore_api_enabled is True
                assert product.lehai_api_enabled is False
                assert product.external_stock == 3

        asyncio.run(verify_only_sumi())
        assert lehai.fetch_product_ids == []
        products_page = client.get("/admin/products")
        assert re.search(r"Lê Hải:\s*<strong>Tắt</strong>", products_page.text)

        price_only = client.post(
            f"/admin/products/{product_id}",
            data=product_form(
                csrf,
                price="34.000",
                supplier_markup="5.000",
                sumistore_api_enabled="1",
            ),
            follow_redirects=False,
        )
        assert price_only.status_code == 303

        async def verify_price_only_updates_markup() -> None:
            async with sessions() as session:
                product = await session.get(Product, product_id)
                assert product is not None
                assert product.supplier_price == 25_000
                assert product.price == 34_000
                assert product.supplier_markup == 9_000
                await apply_supplier_price(session, product, 25_000)
                await session.commit()
                assert product.price == 34_000

        asyncio.run(verify_price_only_updates_markup())

        both_off_form = product_form(csrf, price="32.000", supplier_markup="99.000")
        both_off_form.pop("supplier_markup")
        both_off = client.post(
            f"/admin/products/{product_id}",
            data=both_off_form,
            follow_redirects=False,
        )
        assert both_off.status_code == 303

        async def verify_both_off() -> None:
            async with sessions() as session:
                product = await session.get(Product, product_id)
                assert product is not None
                assert product.sumistore_api_enabled is False
                assert product.lehai_api_enabled is False
                assert product.external_stock == 1
                assert product.price == 32_000
                assert product.supplier_markup == 9_000
                alert = await session.scalar(
                    select(ProductPriceAlert)
                    .where(ProductPriceAlert.product_id == product_id)
                    .order_by(ProductPriceAlert.id.desc())
                    .limit(1)
                )
                assert alert is not None
                assert alert.provider == "admin"
                assert alert.sale_price_before == 34_000
                assert alert.sale_price_after == 32_000

        asyncio.run(verify_both_off())
        products_page = client.get("/admin/products")
        assert "API đang đấu: Đã tắt cả hai" in products_page.text
        assert re.search(r'<span class="stock[^"]*">1</span>', products_page.text)
        disabled_edit_page = client.get(f"/admin/products/{product_id}")
        assert 'id="product-price-label">Giá bán từ kho' in disabled_edit_page.text
        assert re.search(
            r'id="supplier-markup-input"[^>]*disabled', disabled_edit_page.text
        )

        enabled_again = client.post(
            f"/admin/products/{product_id}",
            data=product_form(
                csrf,
                price="32.000",
                supplier_markup="7.000",
                sumistore_api_enabled="1",
            ),
            follow_redirects=False,
        )
        assert enabled_again.status_code == 303

        async def verify_enabled_again() -> None:
            async with sessions() as session:
                product = await session.get(Product, product_id)
                assert product is not None
                assert product.sumistore_api_enabled is True
                assert product.lehai_api_enabled is False
                assert product.supplier_markup == 7_000
                assert product.price == 32_000

        asyncio.run(verify_enabled_again())

    asyncio.run(engine.dispose())


def test_admin_core_ledgers_paginate_all_rows(tmp_path) -> None:
    async def setup_database():
        database_path = (tmp_path / "dashboard-pagination.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(UTC)
        async with sessions() as session:
            users = [
                User(
                    telegram_id=7_000_000_000 + index,
                    full_name=f"PagedUser-{index:03d}",
                    balance=index,
                    has_started=True,
                    created_at=now + timedelta(seconds=index),
                )
                for index in range(205)
            ]
            session.add_all(users)
            category = Category(name_vi="Phân trang", name_en="Pagination")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Sản phẩm phân trang",
                name_en="Pagination product",
                price=1_000,
                fulfillment_source="local",
            )
            session.add(product)
            await session.flush()
            for index in range(1, 106):
                item = InventoryItem(
                    product_id=product.id,
                    encrypted_secret=f"item-{index}",
                    status="sold",
                    sold_at=now,
                )
                session.add(item)
                await session.flush()
                session.add(
                    Order(
                        user_id=users[0].telegram_id,
                        product_id=product.id,
                        inventory_item_id=item.id,
                        amount=1_000,
                        status="completed",
                        delivered_at=now,
                    )
                )
                session.add(
                    Deposit(
                        user_id=users[0].telegram_id,
                        code=f"PAGEDEP{index:03d}",
                        requested_amount=10_000,
                        status="pending",
                        expires_at=now + timedelta(minutes=5),
                    )
                )
                session.add(
                    WalletTransaction(
                        user_id=users[0].telegram_id,
                        kind="admin_adjustment",
                        amount=1,
                        balance_before=index - 1,
                        balance_after=index,
                        reference_type="test",
                        reference_id=str(index),
                        event_key=f"page-ledger-{index}",
                        description=f"Ledger-{index:03d}",
                        created_at=now + timedelta(seconds=index),
                    )
                )
            await session.commit()
            session.add(
                SmsRental(
                    user_id=users[1].telegram_id,
                    shop_order_code="SMS-PAGED-1",
                    provider_order_id="SMS-PROVIDER-PAGED-1",
                    status="success",
                    sale_amount=2_000,
                    cost_amount=1_000,
                    completed_at=now,
                )
            )
            await session.commit()
        return engine, sessions

    engine, sessions = asyncio.run(setup_database())
    encryption_key = Fernet.generate_key().decode()
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=encryption_key,
        dashboard_enabled=True,
        dashboard_username="admin",
        dashboard_password_hash=hash_dashboard_password("dashboard-password"),
        dashboard_session_secret="session-secret-long-enough-for-tests",
    )
    app = create_api(
        settings,
        sessions,
        FakeBot(),  # type: ignore[arg-type]
        SecretCipher(encryption_key),
    )

    with TestClient(app, base_url="https://testserver") as client:
        client.post(
            "/admin/login",
            data={"username": "admin", "password": "dashboard-password"},
        )

        users_page = client.get("/admin/users?page=2")
        assert users_page.status_code == 200
        assert "PagedUser-104" in users_page.text
        assert "PagedUser-204" not in users_page.text
        assert 'href="tg://user?id=7000000104"' in users_page.text
        assert "trên tổng <strong>205</strong> khách hàng" in users_page.text
        assert "Trang <strong>2/3</strong>" in users_page.text

        wallet_users_page = client.get("/admin/users?status=wallet&page=2")
        assert wallet_users_page.status_code == 200
        assert "Khách còn tiền trong ví" in wallet_users_page.text
        assert 'value="wallet" selected' in wallet_users_page.text
        assert "PagedUser-104" in wallet_users_page.text
        assert "PagedUser-000" not in wallet_users_page.text
        assert "trên tổng <strong>204</strong> khách hàng" in wallet_users_page.text
        assert "Tổng số dư trong bộ lọc: <strong>20.910đ</strong>" in wallet_users_page.text

        spent_users_page = client.get("/admin/users?status=spent")
        assert spent_users_page.status_code == 200
        assert 'value="spent" selected' in spent_users_page.text
        assert "PagedUser-000" in spent_users_page.text
        assert "PagedUser-001" in spent_users_page.text
        assert "PagedUser-002" not in spent_users_page.text
        assert "1 lượt SMS" in spent_users_page.text
        assert spent_users_page.text.index("PagedUser-000") < spent_users_page.text.index(
            "PagedUser-001"
        )

        potential_users_page = client.get("/admin/users?status=potential")
        assert potential_users_page.status_code == 200
        assert 'value="potential" selected' in potential_users_page.text
        assert "PagedUser-204" in potential_users_page.text
        assert "PagedUser-203" in potential_users_page.text
        assert "PagedUser-000" not in potential_users_page.text
        assert "PagedUser-001" not in potential_users_page.text
        assert potential_users_page.text.index(
            "PagedUser-204"
        ) < potential_users_page.text.index("PagedUser-203")

        orders_page = client.get("/admin/orders?page=2")
        assert orders_page.status_code == 200
        assert "<code>O5</code>" in orders_page.text
        assert "<code>O105</code>" not in orders_page.text
        assert "trên tổng <strong>105</strong> đơn hàng" in orders_page.text

        payments_page = client.get("/admin/payments?deposit_page=2")
        assert payments_page.status_code == 200
        assert "PAGEDEP005" in payments_page.text
        assert "PAGEDEP105" not in payments_page.text
        assert "trên tổng <strong>105</strong> yêu cầu thanh toán" in payments_page.text

        inventory_page = client.get("/admin/inventory?page=2")
        assert inventory_page.status_code == 200
        assert "trên tổng <strong>105</strong> mục kho" in inventory_page.text

        user_detail_page = client.get("/admin/users/7000000000?page=2")
        assert user_detail_page.status_code == 200
        assert 'href="tg://user?id=7000000000"' in user_detail_page.text
        assert "Ledger-005" in user_detail_page.text
        assert "Ledger-105" not in user_detail_page.text
        assert "trên tổng <strong>105</strong> phát sinh" in user_detail_page.text

    asyncio.run(engine.dispose())


def test_dashboard_login_catalog_inventory_and_balance(tmp_path) -> None:
    async def setup_database():
        database_path = (tmp_path / "dashboard.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            user = User(
                telegram_id=6799701918,
                full_name="Admin test user",
                username="nhattan02",
                balance=50_000,
                has_started=True,
            )
            session.add_all(
                [
                    user,
                    BroadcastLog(
                        admin_id=6799701918,
                        source_chat_id=6799701918,
                        source_message_id=123,
                        total_recipients=10,
                        delivered_count=9,
                        failed_count=1,
                        status="completed",
                    ),
                    SmsRental(
                        user_id=user.telegram_id,
                        shop_order_code="SMS1",
                        provider_order_id="RENTSIM-1",
                        phone_number="+85512345678",
                        status="success",
                        sale_amount=2_000,
                        cost_amount=1_000,
                        otp_code="123456",
                        completed_at=datetime.now(UTC),
                    ),
                ]
            )
            await session.commit()
            api_client = ApiClient(
                owner_user_id=user.telegram_id,
                api_id="VSADMINTEST001",
                encrypted_secret="preview-only",
                rate_limit_per_minute=120,
            )
            session.add(api_client)
            await session.flush()
            session.add(
                ApiRequestAudit(
                    api_client_id=api_client.id,
                    method="GET",
                    path="/v1/products",
                    status_code=500,
                    client_ip="127.0.0.1",
                    duration_ms=123,
                    created_at=datetime.now(UTC),
                )
            )
            await session.commit()
        return engine, sessions

    engine, sessions = asyncio.run(setup_database())
    encryption_key = Fernet.generate_key().decode()
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=encryption_key,
        sepay_enabled=False,
        dashboard_enabled=True,
        dashboard_username="admin",
        dashboard_password_hash=hash_dashboard_password("dashboard-password"),
        dashboard_session_secret="session-secret-long-enough-for-tests",
    )
    bot = FakeBot()
    app = create_api(settings, sessions, bot, SecretCipher(encryption_key))  # type: ignore[arg-type]

    with TestClient(app, base_url="https://testserver") as client:
        protected = client.get("/admin", follow_redirects=False)
        assert protected.status_code == 303
        assert protected.headers["location"] == "/admin/login"

        login_page = client.get("/admin/login")
        assert login_page.status_code == 200
        assert "Đăng nhập quản trị" in login_page.text
        assert login_page.headers["x-frame-options"] == "DENY"
        assert login_page.headers["x-content-type-options"] == "nosniff"

        rejected = client.post(
            "/admin/login",
            data={"username": "admin", "password": "wrong"},
        )
        assert rejected.status_code == 401

        accepted = client.post(
            "/admin/login",
            data={"username": "admin", "password": "dashboard-password"},
            follow_redirects=False,
        )
        assert accepted.status_code == 303
        assert accepted.headers["location"] == "/admin"

        home = client.get("/admin")
        assert home.status_code == 200
        assert "Thành viên mới hôm nay" in home.text
        assert "Lợi nhuận ròng" in home.text
        assert "Giá vốn API" in home.text
        assert "50.000đ" in home.text
        assert "Thuê số SMS" in home.text
        assert 'data-trend-chart' in home.text
        assert home.text.count('class="trend-column"') == 14
        assert home.text.count('aria-pressed="false"') == 14
        assert 'data-trend-detail' in home.text
        assert 'data-trend-revenue' in home.text
        assert 'data-trend-profit' in home.text

        admin_css = client.get("/admin-assets/admin.css")
        assert admin_css.status_code == 200
        assert ".dashboard-grid, .chart-layout, .lower-grid, .detail-grid" in admin_css.text
        assert ".chart-touch-detail" in admin_css.text
        assert "grid-template-columns: repeat(14, 48px)" in admin_css.text
        token_match = re.search(r'name="csrf" value="([^"]+)"', home.text)
        assert token_match is not None
        csrf = token_match.group(1)

        search_without_at = client.get("/admin/users?q=nhattan02")
        search_with_at = client.get("/admin/users?q=%40nhattan02")
        assert search_without_at.status_code == 200
        assert search_with_at.status_code == 200
        assert "Admin test user" in search_without_at.text
        assert "Admin test user" in search_with_at.text
        assert 'href="/admin/users/6799701918"' in search_with_at.text
        assert 'href="https://t.me/nhattan02"' in search_with_at.text

        broadcasts_page = client.get("/admin/broadcasts")
        assert broadcasts_page.status_code == 200
        assert "Toàn bộ lịch sử gửi" in broadcasts_page.text
        assert "Message 123" in broadcasts_page.text
        assert "Hoàn thành" in broadcasts_page.text
        assert "10/10" in broadcasts_page.text
        assert 'href="/admin/broadcasts?tab=admin"' in broadcasts_page.text
        assert "Thông báo mặt hàng giảm giá" not in broadcasts_page.text
        assert broadcasts_page.text.count("<th>Tiến độ</th>") == 1

        sale_broadcasts_page = client.get("/admin/broadcasts?tab=sale")
        assert sale_broadcasts_page.status_code == 200
        assert "Thông báo mặt hàng giảm giá" in sale_broadcasts_page.text
        assert "Hàng mới về tự động" not in sale_broadcasts_page.text
        assert sale_broadcasts_page.text.count("<th>Tiến độ</th>") == 1
        assert sale_broadcasts_page.text.count("<th>Tốc độ</th>") == 1
        assert sale_broadcasts_page.text.count("<th>Thời lượng</th>") == 1

        stock_broadcasts_page = client.get("/admin/broadcasts?tab=stock")
        assert stock_broadcasts_page.status_code == 200
        assert "Hàng mới về tự động" in stock_broadcasts_page.text
        assert "Sale API tự động" not in stock_broadcasts_page.text
        assert stock_broadcasts_page.text.count("<th>Tiến độ</th>") == 1
        assert stock_broadcasts_page.text.count("<th>Tốc độ</th>") == 1
        assert stock_broadcasts_page.text.count("<th>Thời lượng</th>") == 1

        invalid_csrf = client.post(
            "/admin/categories",
            data={"csrf": "invalid", "name_vi": "Sai", "position": "1"},
            follow_redirects=False,
        )
        assert invalid_csrf.status_code == 303

        created_category = client.post(
            "/admin/categories",
            data={
                "csrf": csrf,
                "name_vi": "Tài khoản",
                "name_en": "Accounts",
                "position": "1",
            },
            follow_redirects=False,
        )
        assert created_category.status_code == 303

        empty_category = client.post(
            "/admin/categories",
            data={
                "csrf": csrf,
                "name_vi": "Gian trống",
                "name_en": "Empty",
                "position": "99",
            },
            follow_redirects=False,
        )
        assert empty_category.status_code == 303
        categories_page = client.get("/admin/categories")
        category_forms = re.findall(
            r'<form method="post" action="/admin/categories/\d+" class="category-form">.*?</form>',
            categories_page.text,
            re.DOTALL,
        )
        empty_category_form = next(
            form for form in category_forms if 'value="Gian trống"' in form
        )
        empty_category_id = int(
            re.search(r'action="/admin/categories/(\d+)"', empty_category_form).group(1)
        )  # type: ignore[union-attr]
        deleted_category = client.post(
            f"/admin/categories/{empty_category_id}/delete",
            data={"csrf": csrf},
            follow_redirects=False,
        )
        assert deleted_category.status_code == 303

        payments_page = client.get("/admin/payments")
        assert payments_page.status_code == 200
        assert "Ai nạp, số tiền và thời gian đầy đủ" in payments_page.text

        supplier_audit_page = client.get("/admin/supplier-audit")
        assert supplier_audit_page.status_code == 200
        assert "Giao dịch đáng ngờ" in supplier_audit_page.text
        assert 'action="/admin/supplier-audit/reconcile"' in supplier_audit_page.text
        lehai_audit_page = client.get("/admin/supplier-audit?provider=lehai")
        assert lehai_audit_page.status_code == 200
        assert 'name="provider" value="lehai"' in lehai_audit_page.text
        assert "/admin/supplier-audit?provider=lehai" in lehai_audit_page.text
        assert "/admin/supplier-audit?provider=lehai&kind=refunded" in lehai_audit_page.text
        assert "Lịch sử gọi mua gần đây" in lehai_audit_page.text

        sms_page = client.get("/admin/sms-rentals")
        assert sms_page.status_code == 200
        assert "SMS1" in sms_page.text
        assert "+85512345678" in sms_page.text
        assert "123456" in sms_page.text
        assert "Đang hoạt động" in sms_page.text
        maintenance_on = client.post(
            "/admin/sms-rentals/maintenance",
            data={"csrf": csrf, "mode": "enable"},
            follow_redirects=False,
        )
        assert maintenance_on.status_code == 303
        sms_maintenance_page = client.get("/admin/sms-rentals")
        assert "Đang bảo trì" in sms_maintenance_page.text
        assert "Tắt bảo trì · Mở thuê số" in sms_maintenance_page.text
        maintenance_off = client.post(
            "/admin/sms-rentals/maintenance",
            data={"csrf": csrf, "mode": "disable"},
            follow_redirects=False,
        )
        assert maintenance_off.status_code == 303
        assert "Đang hoạt động" in client.get("/admin/sms-rentals").text

        api_clients_page = client.get("/admin/api-clients")
        assert api_clients_page.status_code == 200
        assert "API đấu kho" in api_clients_page.text
        assert "Request 24 giờ" in api_clients_page.text
        assert "VSADMINTEST001" in api_clients_page.text
        filtered_api_clients = client.get(
            "/admin/api-clients?q=VSADMINTEST001&status=active"
        )
        assert filtered_api_clients.status_code == 200
        assert "Đang hiển thị 1 kết quả phù hợp" in filtered_api_clients.text

        referrals_page = client.get("/admin/referrals")
        assert referrals_page.status_code == 200
        assert "Hoa hồng 2%" in referrals_page.text

        products_page = client.get("/admin/products")
        category_id = int(
            re.search(r'<option value="(\d+)">Tài khoản</option>', products_page.text).group(1)
        )  # type: ignore[union-attr]
        created_product = client.post(
            "/admin/products",
            data={
                "csrf": csrf,
                "category_id": str(category_id),
                "name_vi": "Tài khoản thử nghiệm",
                "name_en": "Test account",
                "price": "25.000",
                "description_vi": "Giao tự động",
                "product_type": "account",
                "allow_quantity": "1",
                "max_quantity": "100",
            },
            follow_redirects=False,
        )
        assert created_product.status_code == 303

        products_page = client.get("/admin/products")
        product_id = int(
            re.search(r'href="/admin/products/(\d+)">Sửa', products_page.text).group(1)  # type: ignore[union-attr]
        )

        created_discount = client.post(
            "/admin/discounts",
            data={
                "csrf": csrf,
                "product_id": str(product_id),
                "code": "TEST5K",
                "discount_type": "fixed",
                "discount_value": "5.000",
                "max_uses": "10",
                "starts_at": "",
                "expires_at": "",
            },
            follow_redirects=False,
        )
        assert created_discount.status_code == 303
        discounts_page = client.get("/admin/discounts")
        assert "TEST5K" in discounts_page.text
        created_quantity_discount = client.post(
            "/admin/quantity-discounts",
            data={
                "csrf": csrf,
                "product_id": str(product_id),
                "min_quantity": ["10", "50"],
                "discount_percent": ["10", "15"],
            },
            follow_redirects=False,
        )
        assert created_quantity_discount.status_code == 303
        created_fixed_quantity_discount = client.post(
            "/admin/quantity-discounts/fixed",
            data={
                "csrf": csrf,
                "product_id": str(product_id),
                "min_quantity": ["20", "40"],
                "discount_amount": ["1000", "2000"],
            },
            follow_redirects=False,
        )
        assert created_fixed_quantity_discount.status_code == 303
        discounts_page = client.get("/admin/discounts")
        assert "+ Thêm mốc" in discounts_page.text
        assert "Giảm số tiền cố định trên mỗi tài khoản" in discounts_page.text
        assert 'name="discount_amount" value="1000" min="1" step="1"' in discounts_page.text
        assert "-1.000đ/1" in discounts_page.text
        quantity_discount_ids = [
            int(value)
            for value in re.findall(
                r'action="/admin/quantity-discounts/(\d+)/toggle"',
                discounts_page.text,
            )
        ]
        assert len(quantity_discount_ids) == 4
        toggled_quantity_discount = client.post(
            f"/admin/quantity-discounts/{quantity_discount_ids[0]}/toggle",
            data={"csrf": csrf},
            follow_redirects=False,
        )
        assert toggled_quantity_discount.status_code == 303
        for quantity_discount_id in quantity_discount_ids:
            deleted_quantity_discount = client.post(
                f"/admin/quantity-discounts/{quantity_discount_id}/delete",
                data={"csrf": csrf},
                follow_redirects=False,
            )
            assert deleted_quantity_discount.status_code == 303
        discount_id = int(
            re.search(
                r'action="/admin/discounts/(\d+)/toggle"',
                discounts_page.text,
            ).group(1)
        )  # type: ignore[union-attr]
        deleted_discount = client.post(
            f"/admin/discounts/{discount_id}/delete",
            data={"csrf": csrf},
            follow_redirects=False,
        )
        assert deleted_discount.status_code == 303

        imported = client.post(
            "/admin/inventory",
            data={
                "csrf": csrf,
                "product_id": str(product_id),
                "items": "account1:password1\naccount2:password2",
                "cost_amount": "7.000",
            },
            follow_redirects=False,
        )
        assert imported.status_code == 303

        stock_zero = client.post(
            f"/admin/products/{product_id}/stock-zero",
            data={"csrf": csrf, "action": "zero"},
            follow_redirects=False,
        )
        assert stock_zero.status_code == 303
        assert stock_zero.headers["location"] == "/admin/products"
        products_page = client.get("/admin/products")
        assert "Tạm khóa · kho thật 2" in products_page.text
        assert ">Mở lại</button>" in products_page.text

        restored_stock = client.post(
            f"/admin/products/{product_id}/stock-zero",
            data={"csrf": csrf, "action": "restore"},
            follow_redirects=False,
        )
        assert restored_stock.status_code == 303
        products_page = client.get("/admin/products")
        assert ">Về 0</button>" in products_page.text
        assert "Tạm khóa · kho thật" not in products_page.text

        inventory_page = client.get("/admin/inventory")
        delete_match = re.search(r'action="/admin/inventory/(\d+)/delete"', inventory_page.text)
        assert delete_match is not None
        deleted = client.post(
            f"/admin/inventory/{delete_match.group(1)}/delete",
            data={"csrf": csrf},
            follow_redirects=False,
        )
        assert deleted.status_code == 303

        product_edit_page = client.get(f"/admin/products/{product_id}")
        assert f'action="/admin/products/{product_id}/delete"' in product_edit_page.text
        deleted_product = client.post(
            f"/admin/products/{product_id}/delete",
            data={"csrf": csrf},
            follow_redirects=False,
        )
        assert deleted_product.status_code == 303
        assert deleted_product.headers["location"] == "/admin/products"

        adjusted = client.post(
            "/admin/users/6799701918/balance",
            data={"csrf": csrf, "amount": "+10.000", "reason": "dashboard-test"},
            follow_redirects=False,
        )
        assert adjusted.status_code == 303
        assert adjusted.headers["location"] == "/admin/users/6799701918"
        assert bot.messages
        assert bot.messages[-1][0][0] == 6799701918
        assert "Số dư ví đã được điều chỉnh" in str(bot.messages[-1][0][1])
        assert "+10.000đ" in str(bot.messages[-1][0][1])
        assert "dashboard-test" in str(bot.messages[-1][0][1])
        assert "60.000đ" in str(bot.messages[-1][0][1])
        user_detail = client.get(adjusted.headers["location"])
        assert user_detail.status_code == 200
        assert "WALLET LEDGER" in user_detail.text
        assert "dashboard-test" in user_detail.text
        assert "50.000" in user_detail.text and "60.000" in user_detail.text

    async def verify_database() -> None:
        async with sessions() as session:
            category_count = int(await session.scalar(select(func.count(Category.id))) or 0)
            product_count = int(await session.scalar(select(func.count(Product.id))) or 0)
            stock_count = int(await session.scalar(select(func.count(InventoryItem.id))) or 0)
            user = await session.get(User, 6799701918)
            adjustment = await session.scalar(select(BalanceAdjustment))
            wallet_transaction = await session.scalar(select(WalletTransaction))
            discount_count = int(await session.scalar(select(func.count(DiscountCode.id))) or 0)
            quantity_discount_count = int(
                await session.scalar(select(func.count(QuantityDiscount.id))) or 0
            )
            assert category_count == 1
            assert product_count == 0
            assert stock_count == 0
            assert user is not None and user.balance == 60_000
            assert adjustment is not None and adjustment.amount == 10_000
            assert wallet_transaction is not None
            assert wallet_transaction.kind == "admin_adjustment"
            assert wallet_transaction.amount == 10_000
            assert wallet_transaction.balance_before == 50_000
            assert wallet_transaction.balance_after == 60_000
            assert discount_count == 0
            assert quantity_discount_count == 0
        await engine.dispose()

    asyncio.run(verify_database())


def test_admin_can_approve_pending_wallet_deposit_once(tmp_path) -> None:
    async def setup_database():
        database_path = (tmp_path / "dashboard-manual-deposit.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            user = User(
                telegram_id=88990011,
                full_name="Manual deposit user",
                username="manualdeposit",
                balance=5_000,
            )
            session.add(user)
            await session.flush()
            deposit = Deposit(
                user_id=user.telegram_id,
                code="NAP88990011MANU",
                requested_amount=20_000,
                status="pending",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            session.add(deposit)
            await session.commit()
            return engine, sessions, deposit.id

    engine, sessions, deposit_id = asyncio.run(setup_database())
    encryption_key = Fernet.generate_key().decode()
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=encryption_key,
        dashboard_enabled=True,
        dashboard_username="admin",
        dashboard_password_hash=hash_dashboard_password("dashboard-password"),
        dashboard_session_secret="session-secret-long-enough-for-tests",
    )
    bot = FakeBot()
    app = create_api(
        settings,
        sessions,
        bot,  # type: ignore[arg-type]
        SecretCipher(encryption_key),
    )

    with TestClient(app, base_url="https://testserver") as client:
        client.post(
            "/admin/login",
            data={"username": "admin", "password": "dashboard-password"},
        )
        payments_page = client.get("/admin/payments")
        csrf_match = re.search(r'name="csrf" value="([^"]+)"', payments_page.text)
        assert csrf_match is not None
        csrf = csrf_match.group(1)
        assert f'action="/admin/payments/deposits/{deposit_id}/approve"' in payments_page.text
        assert "Duyệt +20.000đ" in payments_page.text
        assert "data-async-payment" in payments_page.text

        approved = client.post(
            f"/admin/payments/deposits/{deposit_id}/approve",
            data={"csrf": csrf},
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
            follow_redirects=False,
        )
        duplicate = client.post(
            f"/admin/payments/deposits/{deposit_id}/approve",
            data={"csrf": csrf},
            follow_redirects=False,
        )
        assert approved.status_code == 200
        assert approved.json()["ok"] is True
        assert approved.json()["status"] == "approved"
        assert duplicate.status_code == 303
        assert len(bot.messages) == 1
        assert bot.messages[0][0][0] == 88990011
        assert "Khoản nạp đã được Admin duyệt" in str(bot.messages[0][0][1])

    async def verify_database() -> None:
        async with sessions() as session:
            user = await session.get(User, 88990011)
            deposit = await session.get(Deposit, deposit_id)
            payment_transactions = list(
                await session.scalars(select(PaymentTransaction))
            )
            adjustments = list(await session.scalars(select(BalanceAdjustment)))
            wallet_transactions = list(await session.scalars(select(WalletTransaction)))
            assert user is not None and user.balance == 25_000
            assert deposit is not None and deposit.status == "paid"
            assert len(payment_transactions) == 1
            assert payment_transactions[0].provider_tx_id == f"ADMIN-DEPOSIT-{deposit_id}"
            assert len(adjustments) == 1
            assert len(wallet_transactions) == 1
        await engine.dispose()

    asyncio.run(verify_database())


def test_admin_can_cancel_pending_wallet_deposit_once(tmp_path) -> None:
    async def setup_database():
        database_path = (tmp_path / "dashboard-cancel-deposit.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            user = User(
                telegram_id=88990012,
                full_name="Cancelled deposit user",
                username="canceldeposit",
                balance=5_000,
            )
            session.add(user)
            await session.flush()
            deposit = Deposit(
                user_id=user.telegram_id,
                code="NAP88990012CANC",
                requested_amount=20_000,
                status="pending",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            expired = Deposit(
                user_id=user.telegram_id,
                code="NAP88990012EXPD",
                requested_amount=15_000,
                status="failed",
                failure_reason="expired",
                failed_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) - timedelta(minutes=1),
            )
            session.add_all([deposit, expired])
            await session.flush()
            session.add(
                PaymentTransaction(
                    deposit_id=expired.id,
                    user_id=user.telegram_id,
                    provider_tx_id="EXPIRED-CHECK-88990012",
                    amount=15_000,
                    credit_status="expired",
                )
            )
            await session.commit()
            return engine, sessions, deposit.id, expired.id

    engine, sessions, deposit_id, expired_id = asyncio.run(setup_database())
    encryption_key = Fernet.generate_key().decode()
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=encryption_key,
        dashboard_enabled=True,
        dashboard_username="admin",
        dashboard_password_hash=hash_dashboard_password("dashboard-password"),
        dashboard_session_secret="session-secret-long-enough-for-tests",
    )
    bot = FakeBot()
    app = create_api(
        settings,
        sessions,
        bot,  # type: ignore[arg-type]
        SecretCipher(encryption_key),
    )

    with TestClient(app, base_url="https://testserver") as client:
        client.post(
            "/admin/login",
            data={"username": "admin", "password": "dashboard-password"},
        )
        payments_page = client.get("/admin/payments")
        csrf_match = re.search(r'name="csrf" value="([^"]+)"', payments_page.text)
        assert csrf_match is not None
        csrf = csrf_match.group(1)
        assert f'action="/admin/payments/deposits/{deposit_id}/approve"' in payments_page.text
        assert f'action="/admin/payments/deposits/{deposit_id}/cancel"' not in payments_page.text
        assert f'action="/admin/payments/deposits/{expired_id}/approve"' in payments_page.text
        assert f'action="/admin/payments/deposits/{expired_id}/cancel"' in payments_page.text
        assert "Đã hết hạn" in payments_page.text
        assert ">Hủy<" in payments_page.text
        assert "1 giao dịch không cộng tiền" not in payments_page.text

        cancelled = client.post(
            f"/admin/payments/deposits/{deposit_id}/cancel",
            data={"csrf": csrf},
            follow_redirects=False,
        )
        expired_cancelled = client.post(
            f"/admin/payments/deposits/{expired_id}/cancel",
            data={"csrf": csrf},
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
            follow_redirects=False,
        )
        expired_duplicate = client.post(
            f"/admin/payments/deposits/{expired_id}/cancel",
            data={"csrf": csrf},
            follow_redirects=False,
        )
        assert cancelled.status_code == 303
        assert cancelled.headers["location"] == "/admin/payments"
        assert expired_cancelled.status_code == 200
        assert expired_cancelled.json()["ok"] is True
        assert expired_cancelled.json()["status"] == "cancelled"
        assert expired_duplicate.status_code == 303
        assert len(bot.messages) == 0
        cancelled_page = client.get("/admin/payments")
        assert f'action="/admin/payments/deposits/{expired_id}/approve"' not in cancelled_page.text
        assert f'action="/admin/payments/deposits/{expired_id}/cancel"' not in cancelled_page.text
        assert "Đã hủy" in cancelled_page.text

    async def verify_database() -> None:
        async with sessions() as session:
            user = await session.get(User, 88990012)
            deposit = await session.get(Deposit, deposit_id)
            expired = await session.get(Deposit, expired_id)
            assert user is not None and user.balance == 5_000
            assert deposit is not None and deposit.status == "pending"
            assert deposit.failure_reason is None
            assert deposit.failed_at is None
            assert expired is not None and expired.status == "failed"
            assert expired.failure_reason == "admin_cancelled"
            transaction = await session.scalar(select(PaymentTransaction))
            assert transaction is not None and transaction.credit_status == "expired"
            assert await session.scalar(select(BalanceAdjustment.id)) is None
            assert await session.scalar(select(WalletTransaction.id)) is None
        await engine.dispose()

    asyncio.run(verify_database())


def test_dashboard_shows_sale_alert_history(tmp_path) -> None:
    async def setup_database():
        database_path = (tmp_path / "dashboard-sale-history.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            user = User(telegram_id=68001, full_name="Sale history user", has_started=True)
            session.add_all([category, user])
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus sale",
                name_en="GPT Plus sale",
                price=14_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-SALE-HISTORY",
                supplier_price=12_000,
                supplier_markup=2_000,
                external_stock=4,
                supplier_synced_at=datetime.now(UTC),
            )
            session.add(product)
            await session.flush()
            session.add_all(
                [
                    ProductPriceAlert(
                    product_id=product.id,
                    provider="sumistore",
                    supplier_price_before=15_000,
                    supplier_price_after=12_000,
                    sale_price_before=17_000,
                    sale_price_after=14_000,
                    status="sent",
                    total_recipients=10,
                    delivered_count=9,
                    failed_count=1,
                    sent_at=datetime.now(UTC),
                    ),
                    ProductStockAlert(
                        product_id=product.id,
                        provider="sumistore",
                        stock_before=0,
                        stock_after=4,
                        sale_price=14_000,
                        status="sent",
                        total_recipients=10,
                        delivered_count=10,
                        message_vi="📦 HÀNG MỚI VỀ\n\nSản phẩm: GPT Plus sale",
                        message_en="📦 PRODUCT BACK IN STOCK\n\nProduct: GPT Plus sale",
                        sent_at=datetime.now(UTC),
                    ),
                ]
            )
            await session.commit()
        return engine, sessions

    engine, sessions = asyncio.run(setup_database())
    encryption_key = Fernet.generate_key().decode()
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=encryption_key,
        sepay_enabled=False,
        dashboard_enabled=True,
        dashboard_username="admin",
        dashboard_password_hash=hash_dashboard_password("dashboard-password"),
        dashboard_session_secret="session-secret-long-enough-for-tests",
    )
    app = create_api(settings, sessions, FakeBot(), SecretCipher(encryption_key))  # type: ignore[arg-type]

    with TestClient(app, base_url="https://testserver") as client:
        login = client.post(
            "/admin/login",
            data={"username": "admin", "password": "dashboard-password"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        home = client.get("/admin")
        assert home.status_code == 200
        assert "SALE HISTORY" in home.text
        assert "GPT Plus sale" in home.text
        assert "SP-SALE-HISTORY" in home.text
        assert "Xem đầy đủ" in home.text
        assert "BACK IN STOCK" in home.text
        assert "10/10" in home.text
        sale_broadcasts = client.get("/admin/broadcasts?tab=sale")
        assert sale_broadcasts.status_code == 200
        assert "SALE HISTORY" in sale_broadcasts.text
        assert "GPT Plus sale" in sale_broadcasts.text
        assert "10/10" in sale_broadcasts.text

        stock_broadcasts = client.get("/admin/broadcasts?tab=stock")
        assert stock_broadcasts.status_code == 200
        assert "BACK IN STOCK HISTORY" in stock_broadcasts.text
        assert "GPT Plus sale" in stock_broadcasts.text
        assert "10/10" in stock_broadcasts.text
        assert "Xem nội dung" in stock_broadcasts.text
        assert "HÀNG MỚI VỀ" in stock_broadcasts.text

    asyncio.run(engine.dispose())


def test_admin_can_enable_stock_notifications_without_balance_topup(tmp_path) -> None:
    async def setup_database():
        database_path = (tmp_path / "stock-notification-switch.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus",
                name_en="GPT Plus",
                price=17_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_markup=5_000,
            )
            session.add(product)
            await session.commit()
        return engine, sessions, category.id, product.id

    engine, sessions, category_id, product_id = asyncio.run(setup_database())
    encryption_key = Fernet.generate_key().decode()
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=encryption_key,
        dashboard_enabled=True,
        dashboard_username="admin",
        dashboard_password_hash=hash_dashboard_password("dashboard-password"),
        dashboard_session_secret="session-secret-long-enough-for-tests",
    )
    app = create_api(
        settings,
        sessions,
        FakeBot(),  # type: ignore[arg-type]
        SecretCipher(encryption_key),
    )

    with TestClient(app, base_url="https://testserver") as client:
        client.post(
            "/admin/login",
            data={"username": "admin", "password": "dashboard-password"},
        )
        edit_page = client.get(f"/admin/products/{product_id}")
        csrf = re.search(r'name="csrf" value="([^"]+)"', edit_page.text).group(1)
        assert 'name="notify_stock_without_balance_topup"' in edit_page.text
        assert 'name="sale_notifications_enabled"' in edit_page.text
        assert 'name="stock_notifications_enabled"' in edit_page.text

        updated = client.post(
            f"/admin/products/{product_id}",
            data={
                "csrf": csrf,
                "category_id": str(category_id),
                "name_vi": "GPT Plus",
                "name_en": "GPT Plus",
                "price": "17.000",
                "description_vi": "",
                "description_en": "",
                "product_type": "account",
                "fulfillment_source": "sumistore",
                "supplier_product_id": "SP-GEF55PBV",
                "supplier_markup": "5.000",
                "notification_controls_present": "1",
                "stock_notifications_enabled": "1",
                "notify_stock_without_balance_topup": "1",
                "allow_quantity": "1",
                "max_quantity": "10",
                "active": "1",
            },
            follow_redirects=False,
        )
        assert updated.status_code == 303
        edit_page = client.get(f"/admin/products/{product_id}")
        assert re.search(
            r'name="notify_stock_without_balance_topup"[^>]*checked',
            edit_page.text,
        )
        assert re.search(
            r'name="stock_notifications_enabled"[^>]*checked',
            edit_page.text,
        )
        assert not re.search(
            r'name="sale_notifications_enabled"[^>]*checked',
            edit_page.text,
        )
        products_page = client.get("/admin/products")
        assert "TB hàng về: tăng kho · nghỉ 10 phút" in products_page.text
        assert "TB Sale: tắt" in products_page.text

    async def verify_database() -> None:
        async with sessions() as session:
            product = await session.get(Product, product_id)
            assert product is not None and product.notify_stock_without_balance_topup is True
            assert product.stock_notifications_enabled is True
            assert product.sale_notifications_enabled is False
        await engine.dispose()

    asyncio.run(verify_database())


def test_admin_can_import_recovered_external_inventory(tmp_path) -> None:
    async def setup_database():
        database_path = (tmp_path / "dashboard-external-inventory.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            user = User(telegram_id=68002, full_name="External inventory admin")
            session.add_all([category, user])
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus external",
                name_en="GPT Plus external",
                price=15_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_price=10_000,
                supplier_markup=5_000,
                supplier_available_stock=5,
                external_stock=5,
                active=True,
            )
            session.add(product)
            await session.commit()
        return engine, sessions, product.id

    engine, sessions, product_id = asyncio.run(setup_database())
    encryption_key = Fernet.generate_key().decode()
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=encryption_key,
        sepay_enabled=False,
        dashboard_enabled=True,
        dashboard_username="admin",
        dashboard_password_hash=hash_dashboard_password("dashboard-password"),
        dashboard_session_secret="session-secret-long-enough-for-tests",
    )
    app = create_api(settings, sessions, FakeBot(), SecretCipher(encryption_key))  # type: ignore[arg-type]

    with TestClient(app, base_url="https://testserver") as client:
        login = client.post(
            "/admin/login",
            data={"username": "admin", "password": "dashboard-password"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        inventory_page = client.get("/admin/inventory")
        assert inventory_page.status_code == 200
        assert f'<option value="{product_id}">' in inventory_page.text
        assert 'name="cost_amount"' in inventory_page.text
        assert 'name="lock_sale_price"' in inventory_page.text
        assert 'name="notify_stock_arrival"' in inventory_page.text
        assert 'data-inventory-items' in inventory_page.text
        assert 'data-inventory-count' in inventory_page.text
        assert "Mỗi tài khoản một dòng" in inventory_page.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', inventory_page.text).group(1)
        imported = client.post(
            "/admin/inventory",
            data={
                "csrf": csrf,
                "product_id": str(product_id),
                "items": "mics.retry-6h+5frux@icloud.com|password|key",
                "cost_amount": "8.000",
                "lock_sale_price": "1",
                "notify_stock_arrival": "1",
            },
            follow_redirects=False,
        )
        assert imported.status_code == 303
        assert "Kho nhập" in client.get("/admin/broadcasts?tab=stock").text
        assert "Kho nhập" in client.get("/admin").text

        checked_import = client.post(
            "/admin/inventory",
            data={
                "csrf": csrf,
                "product_id": str(product_id),
                "items": (
                    "MICS.RETRY-6H+5FRUX@ICLOUD.COM|new-password|new-key\n"
                    "clean-account@example.com|password|key\n"
                    "clean-account@example.com|another-password|another-key"
                ),
                "cost_amount": "9.000",
            },
            follow_redirects=False,
        )
        assert checked_import.status_code == 303
        duplicate_page = client.get("/admin/inventory")
        assert "MICS.RETRY-6H+5FRUX@ICLOUD.COM" in duplicate_page.text
        assert "clean-account@example.com" in duplicate_page.text
        assert "Đã tồn tại trong kho/lịch sử bán" in duplicate_page.text
        assert "Trùng trong chính lần nhập này" in duplicate_page.text

    async def verify_database() -> None:
        async with sessions() as session:
            items = list(
                await session.scalars(select(InventoryItem).order_by(InventoryItem.id))
            )
            assert len(items) == 2
            assert items[0].product_id == product_id
            assert items[0].cost_amount == 8_000
            assert items[0].account_fingerprint is not None
            assert items[1].cost_amount == 9_000
            assert items[1].account_fingerprint is not None
            alerts = list(
                await session.scalars(
                    select(InventoryDuplicateAlert).order_by(InventoryDuplicateAlert.id)
                )
            )
            assert [alert.reason for alert in alerts] == [
                "duplicate_existing",
                "duplicate_in_import",
            ]
            assert alerts[0].existing_inventory_item_id == items[0].id
            assert alerts[1].existing_inventory_item_id is None
            product = await session.get(Product, product_id)
            assert product is not None and product.price_lock_enabled is True
            assert product.external_stock == 7
            alert = await session.scalar(select(ProductStockAlert))
            assert alert is not None
            assert alert.provider == "inventory"
            assert alert.stock_before == 5
            assert alert.stock_after == 6
        await engine.dispose()

    asyncio.run(verify_database())


def test_deleting_unlocked_external_inventory_recalculates_stock(tmp_path) -> None:
    async def setup_database():
        database_path = (tmp_path / "dashboard-delete-external-inventory.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus external",
                name_en="GPT Plus external",
                price=100_000,
                fulfillment_source="lehai",
                supplier_product_id="gptap_bhf",
                supplier_available_stock=0,
                external_stock=9,
                price_lock_enabled=False,
                active=True,
            )
            session.add(product)
            await session.flush()
            item = InventoryItem(
                product_id=product.id,
                encrypted_secret="test-secret",
                cost_amount=95_000,
            )
            session.add(item)
            await session.commit()
            return engine, sessions, product.id, item.id

    engine, sessions, product_id, item_id = asyncio.run(setup_database())
    encryption_key = Fernet.generate_key().decode()
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=encryption_key,
        dashboard_enabled=True,
        dashboard_username="admin",
        dashboard_password_hash=hash_dashboard_password("dashboard-password"),
        dashboard_session_secret="session-secret-long-enough-for-tests",
    )
    app = create_api(
        settings,
        sessions,
        FakeBot(),  # type: ignore[arg-type]
        SecretCipher(encryption_key),
    )

    with TestClient(app, base_url="https://testserver") as client:
        client.post(
            "/admin/login",
            data={"username": "admin", "password": "dashboard-password"},
        )
        inventory_page = client.get("/admin/inventory")
        csrf = re.search(r'name="csrf" value="([^"]+)"', inventory_page.text).group(1)
        deleted = client.post(
            f"/admin/inventory/{item_id}/delete",
            data={"csrf": csrf},
            follow_redirects=False,
        )
        assert deleted.status_code == 303

    async def verify_database() -> None:
        async with sessions() as session:
            product = await session.get(Product, product_id)
            assert product is not None
            assert product.price_lock_enabled is False
            assert product.external_stock == 0
            assert await session.get(InventoryItem, item_id) is None
        await engine.dispose()

    asyncio.run(verify_database())


def test_admin_withdraws_inventory_for_warranty_without_partial_or_resale(tmp_path) -> None:
    encryption_key = Fernet.generate_key().decode()
    cipher = SecretCipher(encryption_key)

    async def setup_database():
        database_path = (tmp_path / "dashboard-withdraw-inventory.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            user = User(
                telegram_id=68123,
                full_name="Warranty inventory buyer",
                balance=200_000,
            )
            session.add_all([category, user])
            await session.flush()
            local_product = Product(
                category_id=category.id,
                name_vi="GPT Plus kho nhập",
                name_en="GPT Plus local stock",
                price=40_000,
                fulfillment_source="local",
                allow_quantity=True,
                max_quantity=10,
                active=True,
            )
            external_product = Product(
                category_id=category.id,
                name_vi="GPT Plus kết hợp API",
                name_en="GPT Plus mixed stock",
                price=45_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_price=40_000,
                supplier_markup=5_000,
                supplier_available_stock=4,
                external_stock=6,
                price_lock_enabled=True,
                active=True,
            )
            session.add_all([local_product, external_product])
            await session.flush()
            local_secrets = [
                "local-one@example.com|pass-one",
                "local-two@example.com|pass-two",
                "local-three@example.com|pass-three",
            ]
            external_secrets = [
                "external-one@example.com|pass-one",
                "external-two@example.com|pass-two",
            ]
            session.add_all(
                [
                    InventoryItem(
                        product_id=local_product.id,
                        encrypted_secret=cipher.encrypt(secret),
                        cost_amount=35_000,
                    )
                    for secret in local_secrets
                ]
                + [
                    InventoryItem(
                        product_id=external_product.id,
                        encrypted_secret=cipher.encrypt(secret),
                        cost_amount=38_000,
                    )
                    for secret in external_secrets
                ]
            )
            await session.commit()
            return (
                engine,
                sessions,
                local_product.id,
                external_product.id,
                local_secrets,
                external_secrets,
            )

    (
        engine,
        sessions,
        local_product_id,
        external_product_id,
        local_secrets,
        external_secrets,
    ) = asyncio.run(setup_database())
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=encryption_key,
        dashboard_enabled=True,
        dashboard_username="admin",
        dashboard_password_hash=hash_dashboard_password("dashboard-password"),
        dashboard_session_secret="session-secret-long-enough-for-tests",
    )
    app = create_api(settings, sessions, FakeBot(), cipher)  # type: ignore[arg-type]

    with TestClient(app, base_url="https://testserver") as anonymous_client:
        protected = anonymous_client.post(
            "/admin/inventory/withdraw",
            data={"csrf": "invalid", "product_id": local_product_id, "quantity": 1},
            follow_redirects=False,
        )
        assert protected.status_code == 303
        assert protected.headers["location"] == "/admin/login"

    withdrawal_locations: list[str] = []
    with TestClient(app, base_url="https://testserver") as client:
        client.post(
            "/admin/login",
            data={"username": "admin", "password": "dashboard-password"},
        )
        inventory_page = client.get("/admin/inventory")
        assert inventory_page.status_code == 200
        assert "Rút hàng bảo hành" in inventory_page.text
        assert "GPT Plus kho nhập · kho nhập 3" in inventory_page.text
        assert all(secret not in inventory_page.text for secret in local_secrets)
        csrf = re.search(r'name="csrf" value="([^"]+)"', inventory_page.text).group(1)

        invalid_csrf = client.post(
            "/admin/inventory/withdraw",
            data={
                "csrf": "invalid",
                "product_id": str(local_product_id),
                "quantity": "1",
            },
            follow_redirects=False,
        )
        assert invalid_csrf.status_code == 303

        local_withdrawal = client.post(
            "/admin/inventory/withdraw",
            data={
                "csrf": csrf,
                "product_id": str(local_product_id),
                "quantity": "2",
                "reason": "Bảo hành đơn BTEST123",
            },
            follow_redirects=False,
        )
        assert local_withdrawal.status_code == 303
        assert re.fullmatch(
            r"/admin/inventory/withdrawals/WD[A-F0-9]{16}",
            local_withdrawal.headers["location"],
        )
        withdrawal_locations.append(local_withdrawal.headers["location"])
        local_detail = client.get(local_withdrawal.headers["location"])
        assert local_detail.status_code == 200
        assert local_detail.headers["cache-control"] == "no-store, private"
        assert "Sao chép tất cả" in local_detail.text
        assert "Bảo hành đơn BTEST123" in local_detail.text
        assert local_secrets[0] in local_detail.text
        assert local_secrets[1] in local_detail.text
        assert local_secrets[2] not in local_detail.text

        partial_attempt = client.post(
            "/admin/inventory/withdraw",
            data={
                "csrf": csrf,
                "product_id": str(local_product_id),
                "quantity": "2",
            },
            follow_redirects=False,
        )
        assert partial_attempt.status_code == 303
        assert partial_attempt.headers["location"] == "/admin/inventory"
        partial_page = client.get("/admin/inventory")
        assert "không rút một phần" in partial_page.text
        assert local_secrets[0] not in partial_page.text
        assert withdrawal_locations[0].rsplit("/", 1)[-1] in partial_page.text
        assert "Đã rút bảo hành" in partial_page.text

        external_withdrawal = client.post(
            "/admin/inventory/withdraw",
            data={
                "csrf": csrf,
                "product_id": str(external_product_id),
                "quantity": "2",
                "reason": "Giữ hàng đổi bảo hành",
            },
            follow_redirects=False,
        )
        assert external_withdrawal.status_code == 303
        withdrawal_locations.append(external_withdrawal.headers["location"])
        external_detail = client.get(external_withdrawal.headers["location"])
        assert all(secret in external_detail.text for secret in external_secrets)

    with TestClient(app, base_url="https://testserver") as anonymous_client:
        protected_detail = anonymous_client.get(
            withdrawal_locations[0], follow_redirects=False
        )
        assert protected_detail.status_code == 303
        assert protected_detail.headers["location"] == "/admin/login"

    async def verify_database_and_resale_guard() -> None:
        async with sessions() as session:
            local_items = list(
                await session.scalars(
                    select(InventoryItem)
                    .where(InventoryItem.product_id == local_product_id)
                    .order_by(InventoryItem.id)
                )
            )
            assert [item.status for item in local_items] == [
                "withdrawn",
                "withdrawn",
                "available",
            ]
            assert local_items[0].withdrawal_code == local_items[1].withdrawal_code
            assert local_items[0].withdrawn_by == "admin"
            assert local_items[0].withdrawal_reason == "Bảo hành đơn BTEST123"
            assert local_items[0].withdrawn_at is not None

            external_items = list(
                await session.scalars(
                    select(InventoryItem)
                    .where(InventoryItem.product_id == external_product_id)
                    .order_by(InventoryItem.id)
                )
            )
            assert [item.status for item in external_items] == ["withdrawn", "withdrawn"]
            external_product = await session.get(Product, external_product_id)
            assert external_product is not None
            assert external_product.supplier_available_stock == 4
            assert external_product.external_stock == 4
            assert external_product.price_lock_enabled is False
            assert int(await session.scalar(select(func.count(Order.id))) or 0) == 0

        first_purchase = await purchase_product(
            sessions,
            68123,
            local_product_id,
            cipher,
        )
        assert first_purchase.ok is True
        assert first_purchase.secrets == [local_secrets[2]]
        second_purchase = await purchase_product(
            sessions,
            68123,
            local_product_id,
            cipher,
        )
        assert second_purchase.ok is False
        assert second_purchase.message == "out_of_stock"

        async with sessions() as session:
            withdrawn_items = list(
                await session.scalars(
                    select(InventoryItem).where(
                        InventoryItem.product_id == local_product_id,
                        InventoryItem.status == "withdrawn",
                    )
                )
            )
            assert len(withdrawn_items) == 2
            assert int(await session.scalar(select(func.count(Order.id))) or 0) == 1
        await engine.dispose()

    asyncio.run(verify_database_and_resale_guard())


def test_admin_can_withdraw_warranty_accounts_directly_from_supplier_api(tmp_path) -> None:
    encryption_key = Fernet.generate_key().decode()
    cipher = SecretCipher(encryption_key)

    async def setup_database():
        database_path = (tmp_path / "dashboard-api-warranty-withdrawal.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus API warranty",
                name_en="GPT Plus API warranty",
                price=40_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_price=30_000,
                supplier_markup=10_000,
                supplier_available_stock=8,
                external_stock=8,
                active=True,
            )
            session.add(product)
            await session.commit()
            return engine, sessions, product.id

    engine, sessions, product_id = asyncio.run(setup_database())
    supplier = DashboardBuyingSupplier(price=30_000, stock=8, balance=300_000)
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=encryption_key,
        dashboard_enabled=True,
        dashboard_username="admin",
        dashboard_password_hash=hash_dashboard_password("dashboard-password"),
        dashboard_session_secret="session-secret-long-enough-for-tests",
    )
    app = create_api(
        settings,
        sessions,
        FakeBot(),  # type: ignore[arg-type]
        cipher,
        supplier_client=supplier,  # type: ignore[arg-type]
    )

    with TestClient(app, base_url="https://testserver") as client:
        client.post(
            "/admin/login",
            data={"username": "admin", "password": "dashboard-password"},
        )
        inventory_page = client.get("/admin/inventory")
        assert inventory_page.status_code == 200
        assert "GPT Plus API warranty · kho nhập 0 · API 8" in inventory_page.text
        assert 'name="source"' in inventory_page.text
        assert "Chỉ API đang đấu" in inventory_page.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', inventory_page.text).group(1)

        withdrawn = client.post(
            "/admin/inventory/withdraw",
            data={
                "csrf": csrf,
                "product_id": str(product_id),
                "quantity": "2",
                "source": "api",
                "reason": "Đổi bảo hành từ nguồn API",
            },
            follow_redirects=False,
        )
        assert withdrawn.status_code == 303
        assert re.fullmatch(
            r"/admin/inventory/withdrawals/WD[A-F0-9]{16}",
            withdrawn.headers["location"],
        )
        withdrawal_code = withdrawn.headers["location"].rsplit("/", 1)[-1]
        detail = client.get(withdrawn.headers["location"])
        assert detail.status_code == 200
        assert "api-warranty-1-1@example.com|password-1" in detail.text
        assert "api-warranty-1-2@example.com|password-2" in detail.text
        assert "API-WARRANTY-1" in detail.text
        assert "Đổi bảo hành từ nguồn API" in detail.text
        assert "60.000đ" in detail.text

        inventory_after = client.get("/admin/inventory")
        assert f"{withdrawal_code}" in inventory_after.text
        assert "API 6" in inventory_after.text
        assert ">API</span>" in inventory_after.text

        insufficient = client.post(
            "/admin/inventory/withdraw",
            data={
                "csrf": csrf,
                "product_id": str(product_id),
                "quantity": "7",
                "source": "api",
            },
            follow_redirects=False,
        )
        assert insufficient.status_code == 303
        insufficient_page = client.get("/admin/inventory")
        assert "Không có một nguồn API riêng lẻ nào đủ số lượng" in insufficient_page.text

    async def verify_database() -> None:
        async with sessions() as session:
            items = list(
                await session.scalars(
                    select(InventoryItem).order_by(InventoryItem.id)
                )
            )
            assert len(items) == 2
            assert all(item.status == "withdrawn" for item in items)
            assert all(item.withdrawal_code == withdrawal_code for item in items)
            assert all(item.supplier_provider == "sumistore" for item in items)
            assert all(item.supplier_order_code == "API-WARRANTY-1" for item in items)
            assert all(item.cost_amount == 30_000 for item in items)

            product = await session.get(Product, product_id)
            assert product is not None
            assert product.supplier_available_stock == 6
            assert product.external_stock == 6

            audit = await session.scalar(select(SupplierBalanceTransaction))
            assert audit is not None
            assert audit.kind == "purchase"
            assert audit.amount == -60_000
            assert audit.shop_order_code == withdrawal_code
            assert audit.supplier_order_code == "API-WARRANTY-1"

            attempt = await session.scalar(select(SupplierPurchaseAttempt))
            assert attempt is not None
            assert attempt.status == "succeeded"
            assert attempt.supplier_order_code == "API-WARRANTY-1"
            assert attempt.request_key == f"warranty-{withdrawal_code.lower()}"
            assert int(await session.scalar(select(func.count(Order.id))) or 0) == 0
        await engine.dispose()

    asyncio.run(verify_database())
    assert supplier.buy_calls == [("SP-GEF55PBV", 2)]


def test_ambiguous_api_warranty_withdrawal_is_linked_to_recovery(tmp_path) -> None:
    encryption_key = Fernet.generate_key().decode()
    cipher = SecretCipher(encryption_key)

    async def setup_database():
        database_path = (tmp_path / "dashboard-api-warranty-recovery.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus API recovery",
                name_en="GPT Plus API recovery",
                price=40_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-AMBIGUOUS-WARRANTY",
                supplier_price=30_000,
                supplier_available_stock=3,
                external_stock=3,
                active=True,
            )
            session.add(product)
            await session.commit()
            return engine, sessions, product.id

    engine, sessions, product_id = asyncio.run(setup_database())
    supplier = AmbiguousDashboardSupplier(price=30_000, stock=3, balance=100_000)
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=encryption_key,
        dashboard_enabled=True,
        dashboard_username="admin",
        dashboard_password_hash=hash_dashboard_password("dashboard-password"),
        dashboard_session_secret="session-secret-long-enough-for-tests",
    )
    app = create_api(
        settings,
        sessions,
        FakeBot(),  # type: ignore[arg-type]
        cipher,
        supplier_client=supplier,  # type: ignore[arg-type]
    )

    with TestClient(app, base_url="https://testserver") as client:
        client.post(
            "/admin/login",
            data={"username": "admin", "password": "dashboard-password"},
        )
        inventory_page = client.get("/admin/inventory")
        csrf = re.search(r'name="csrf" value="([^"]+)"', inventory_page.text).group(1)
        response = client.post(
            "/admin/inventory/withdraw",
            data={
                "csrf": csrf,
                "product_id": str(product_id),
                "quantity": "1",
                "source": "api",
                "reason": "Bảo hành cần thu hồi tự động",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/inventory"
        result_page = client.get("/admin/inventory")
        assert "đang chờ đối soát" in result_page.text
        assert "không bấm lại" in result_page.text
        assert "tự xuất hiện trong lịch sử rút bảo hành" in result_page.text
        assert "Rút bảo hành đang chờ đối soát" in result_page.text
        assert re.search(r"WD[A-F0-9]{16}", result_page.text)

    async def verify_recovery_link() -> None:
        async with sessions() as session:
            recovery = await session.scalar(select(SupplierRecoveryRequest))
            assert recovery is not None
            assert recovery.status == "pending"
            assert re.fullmatch(
                r"WD[A-F0-9]{16}",
                recovery.inventory_withdrawal_code or "",
            )
            assert recovery.inventory_withdrawn_by == "admin"
            assert (
                recovery.inventory_withdrawal_reason
                == "Bảo hành cần thu hồi tự động"
            )
            assert recovery.request_key == (
                f"warranty-{recovery.inventory_withdrawal_code.lower()}"
            )
            assert int(await session.scalar(select(func.count(InventoryItem.id))) or 0) == 0
        await engine.dispose()

    asyncio.run(verify_recovery_link())
    assert supplier.buy_calls == [("SP-AMBIGUOUS-WARRANTY", 1)]


def test_dashboard_groups_multi_item_purchase_as_one_order(tmp_path) -> None:
    async def setup_database():
        database_path = (tmp_path / "grouped-orders.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        encryption_key = Fernet.generate_key().decode()
        cipher = SecretCipher(encryption_key)
        async with sessions() as session:
            category = Category(name_vi="Tài khoản", name_en="Accounts")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="ChatGPT Plus",
                name_en="ChatGPT Plus",
                price=20_000,
                fulfillment_source="sumistore",
            )
            user = User(
                telegram_id=10001,
                full_name="Grouped Buyer",
                username="groupedbuyer",
                has_started=True,
            )
            session.add_all([product, user])
            await session.flush()
            items = [
                InventoryItem(
                    product_id=product.id,
                    encrypted_secret=cipher.encrypt(secret),
                    supplier_provider="lehai",
                    status="sold",
                )
                for secret in ("account-one:secret", "account-two:secret")
            ]
            session.add_all(items)
            await session.flush()
            session.add_all(
                [
                    Order(
                        user_id=user.telegram_id,
                        product_id=product.id,
                        product_name_vi="Tên sản phẩm lúc mua",
                        product_name_en="Product name at purchase",
                        inventory_item_id=item.id,
                        amount=20_000,
                        cost_amount=15_000,
                        batch_code="B-SHOP-123",
                        supplier_order_code="API-ORDER-999",
                        supplier_provider="lehai",
                        status="completed",
                    )
                    for item in items
                ]
            )
            await session.commit()
        return engine, sessions, encryption_key

    engine, sessions, encryption_key = asyncio.run(setup_database())
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=encryption_key,
        sepay_enabled=False,
        dashboard_enabled=True,
        dashboard_username="admin",
        dashboard_password_hash=hash_dashboard_password("dashboard-password"),
        dashboard_session_secret="session-secret-long-enough-for-tests",
    )
    app = create_api(settings, sessions, FakeBot(), SecretCipher(encryption_key))  # type: ignore[arg-type]

    with TestClient(app, base_url="https://testserver") as client:
        client.post(
            "/admin/login",
            data={"username": "admin", "password": "dashboard-password"},
        )
        home = client.get("/admin")
        assert "1 đơn hàng" in home.text
        assert re.search(r"<dt>Nick [^<]+</dt><dd>2</dd>", home.text)
        assert "2 nick trong tháng" in home.text

        orders_page = client.get("/admin/orders")
        assert orders_page.text.count("B-SHOP-123") == 1
        assert "Mã API <code>API-ORDER-999</code>" in orders_page.text
        assert "2 tài khoản" in orders_page.text
        assert "40.000đ" in orders_page.text
        assert "Tên sản phẩm lúc mua" in orders_page.text
        assert re.search(r'<span class="status wait">Lê Hải</span>', orders_page.text)
        assert "B-SHOP-123" in client.get(
            "/admin/orders", params={"q": "@groupedbuyer"}
        ).text
        assert "B-SHOP-123" in client.get(
            "/admin/orders", params={"q": "Tên sản phẩm lúc mua"}
        ).text
        assert "B-SHOP-123" in client.get("/admin/orders?source=lehai").text
        assert "B-SHOP-123" not in client.get("/admin/orders?source=sumistore").text
        order_id = int(
            re.search(r'href="/admin/orders/(\d+)">Mở đơn</a>', orders_page.text).group(1)  # type: ignore[union-attr]
        )

        detail = client.get(f"/admin/orders/{order_id}")
        assert "Đơn B-SHOP-123" in detail.text
        assert "Mã đơn API" in detail.text
        assert "API-ORDER-999" in detail.text
        assert "Nguồn hàng</dt><dd>Lê Hải" in detail.text
        assert "Tên sản phẩm lúc mua" in detail.text
        assert "account-one:secret" in detail.text
        assert "account-two:secret" in detail.text

    asyncio.run(engine.dispose())
