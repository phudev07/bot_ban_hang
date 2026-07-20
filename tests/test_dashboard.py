import asyncio
import re
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import create_api
from app.config import Settings
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
    InventoryItem,
    Order,
    Product,
    ProductPriceAlert,
    ProductStockAlert,
    QuantityDiscount,
    SmsRental,
    User,
)
from app.utils import SecretCipher


class FakeBot:
    async def send_message(self, *_args, **_kwargs) -> None:
        return None


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
    app = create_api(settings, sessions, FakeBot(), SecretCipher(encryption_key))  # type: ignore[arg-type]

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
        token_match = re.search(r'name="csrf" value="([^"]+)"', home.text)
        assert token_match is not None
        csrf = token_match.group(1)

        broadcasts_page = client.get("/admin/broadcasts")
        assert broadcasts_page.status_code == 200
        assert "Toàn bộ lịch sử gửi" in broadcasts_page.text
        assert "Message 123" in broadcasts_page.text
        assert "Hoàn thành" in broadcasts_page.text
        assert "10/10" in broadcasts_page.text
        assert 'href="/admin/broadcasts?tab=admin"' in broadcasts_page.text
        assert "Sale API tự động" not in broadcasts_page.text
        assert broadcasts_page.text.count("<th>Tiến độ</th>") == 1

        sale_broadcasts_page = client.get("/admin/broadcasts?tab=sale")
        assert sale_broadcasts_page.status_code == 200
        assert "Sale API tự động" in sale_broadcasts_page.text
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
        assert "Hoa hồng 5%" in referrals_page.text

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
        discounts_page = client.get("/admin/discounts")
        assert "+ Thêm mốc" in discounts_page.text
        quantity_discount_ids = [
            int(value)
            for value in re.findall(
                r'action="/admin/quantity-discounts/(\d+)/toggle"',
                discounts_page.text,
            )
        ]
        assert len(quantity_discount_ids) == 2
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
            },
            follow_redirects=False,
        )
        assert imported.status_code == 303

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
            data={"csrf": csrf, "amount": "+10.000", "reason": "Kiểm thử dashboard"},
            follow_redirects=False,
        )
        assert adjusted.status_code == 303

    async def verify_database() -> None:
        async with sessions() as session:
            category_count = int(await session.scalar(select(func.count(Category.id))) or 0)
            product_count = int(await session.scalar(select(func.count(Product.id))) or 0)
            stock_count = int(await session.scalar(select(func.count(InventoryItem.id))) or 0)
            user = await session.get(User, 6799701918)
            adjustment = await session.scalar(select(BalanceAdjustment))
            discount_count = int(await session.scalar(select(func.count(DiscountCode.id))) or 0)
            quantity_discount_count = int(
                await session.scalar(select(func.count(QuantityDiscount.id))) or 0
            )
            assert category_count == 1
            assert product_count == 0
            assert stock_count == 0
            assert user is not None and user.balance == 60_000
            assert adjustment is not None and adjustment.amount == 10_000
            assert discount_count == 0
            assert quantity_discount_count == 0
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
        assert "API SALE HISTORY" in sale_broadcasts.text
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
        csrf = re.search(r'name="csrf" value="([^"]+)"', inventory_page.text).group(1)
        imported = client.post(
            "/admin/inventory",
            data={
                "csrf": csrf,
                "product_id": str(product_id),
                "items": "mics.retry-6h+5frux@icloud.com|password|key",
            },
            follow_redirects=False,
        )
        assert imported.status_code == 303

    async def verify_database() -> None:
        async with sessions() as session:
            item = await session.scalar(select(InventoryItem))
            assert item is not None
            assert item.product_id == product_id
            assert item.cost_amount == 10_000
        await engine.dispose()

    asyncio.run(verify_database())


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
            user = User(telegram_id=10001, full_name="Grouped Buyer", has_started=True)
            session.add_all([product, user])
            await session.flush()
            items = [
                InventoryItem(
                    product_id=product.id,
                    encrypted_secret=cipher.encrypt(secret),
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
                        inventory_item_id=item.id,
                        amount=20_000,
                        cost_amount=15_000,
                        batch_code="B-SHOP-123",
                        supplier_order_code="API-ORDER-999",
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
        order_id = int(
            re.search(r'href="/admin/orders/(\d+)">Mở đơn</a>', orders_page.text).group(1)  # type: ignore[union-attr]
        )

        detail = client.get(f"/admin/orders/{order_id}")
        assert "Đơn B-SHOP-123" in detail.text
        assert "Mã đơn API" in detail.text
        assert "API-ORDER-999" in detail.text
        assert "account-one:secret" in detail.text
        assert "account-two:secret" in detail.text

    asyncio.run(engine.dispose())
