import asyncio
import re

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
    Category,
    DiscountCode,
    InventoryItem,
    Order,
    Product,
    User,
)
from app.utils import SecretCipher


class FakeBot:
    async def send_message(self, *_args, **_kwargs) -> None:
        return None


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
        protected = client.get("/admin", follow_redirects=False)
        assert protected.status_code == 303
        assert protected.headers["location"] == "/admin/login"

        login_page = client.get("/admin/login")
        assert login_page.status_code == 200
        assert "Đăng nhập quản trị" in login_page.text

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
        assert "Lợi nhuận gộp" in home.text
        assert "Giá vốn API" in home.text
        assert "50.000đ" in home.text
        token_match = re.search(r'name="csrf" value="([^"]+)"', home.text)
        assert token_match is not None
        csrf = token_match.group(1)

        broadcasts_page = client.get("/admin/broadcasts")
        assert broadcasts_page.status_code == 200
        assert "100 lần gửi gần nhất" in broadcasts_page.text
        assert "Message 123" in broadcasts_page.text

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

        router_tokens_page = client.get("/admin/router-tokens")
        assert router_tokens_page.status_code == 200
        assert "Đơn cấp key và trạng thái retry" in router_tokens_page.text
        assert "Khách còn có thể dùng" in router_tokens_page.text
        assert "Có thể bán thêm" in router_tokens_page.text

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
                "max_quantity": "10",
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
            assert category_count == 1
            assert product_count == 0
            assert stock_count == 0
            assert user is not None and user.balance == 60_000
            assert adjustment is not None and adjustment.amount == 10_000
            assert discount_count == 0
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
