import asyncio
import hashlib
import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.api import create_api
from app.config import Settings
from app.database import Base
from app.models import (
    ApiOrderRequest,
    ApiRequestAudit,
    Category,
    FlashSaleCampaign,
    InventoryItem,
    Order,
    Product,
    ReferralReward,
    SellerPrice,
    User,
)
from app.partner_services import api_signature, ensure_api_client, rotate_api_secret
from app.services import PurchaseResult
from app.public_api import client_ip, order_payload
from app.utils import SecretCipher


class FakeBot:
    async def send_message(self, *_args, **_kwargs) -> None:
        return None


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, int | str] = {}

    async def set(self, key: str, value: str, **kwargs):
        if kwargs.get("nx") and key in self.values:
            return False
        self.values[key] = value
        return True

    async def incr(self, key: str) -> int:
        value = int(self.values.get(key, 0)) + 1
        self.values[key] = value
        return value

    async def expire(self, _key: str, _seconds: int) -> bool:
        return True

    async def aclose(self) -> None:
        return None


def test_order_payload_uses_the_name_saved_at_purchase_time() -> None:
    cipher = SecretCipher(Fernet.generate_key().decode())
    product = Product(
        id=77,
        category_id=1,
        name_vi="Tên sản phẩm hiện tại",
        name_en="Current product name",
        price=20_000,
    )
    item = InventoryItem(
        id=88,
        product_id=product.id,
        encrypted_secret=cipher.encrypt("account|password"),
    )
    order = Order(
        id=99,
        user_id=123,
        product_id=product.id,
        product_name_vi="Tên lúc mua",
        product_name_en="Name at purchase",
        inventory_item_id=item.id,
        amount=20_000,
        discount_amount=0,
        sales_channel="api",
        status="completed",
        created_at=datetime.now(UTC),
        delivered_at=datetime.now(UTC),
        product=product,
        inventory_item=item,
    )

    payload = order_payload([order], cipher)

    assert payload["product"] == {"id": 77, "name": "Tên lúc mua"}


def test_order_payload_exposes_mixed_unit_prices_without_false_unit_price() -> None:
    cipher = SecretCipher(Fernet.generate_key().decode())
    product = Product(id=78, category_id=1, name_vi="Mixed", name_en="Mixed", price=40_000)
    orders: list[Order] = []
    for index, amount in enumerate((35_000, 36_000), start=1):
        item = InventoryItem(
            id=90 + index,
            product_id=product.id,
            encrypted_secret=cipher.encrypt(f"account-{index}|password"),
        )
        orders.append(
            Order(
                id=100 + index,
                user_id=123,
                product_id=product.id,
                product_name_vi="Mixed",
                product_name_en="Mixed",
                inventory_item_id=item.id,
                amount=amount,
                discount_amount=0,
                sales_channel="api",
                status="completed",
                created_at=datetime.now(UTC),
                delivered_at=datetime.now(UTC),
                product=product,
                inventory_item=item,
            )
        )

    payload = order_payload(orders, cipher)

    assert payload["unit_price"] is None
    assert payload["unit_prices"] == [35_000, 36_000]
    assert payload["total_amount"] == 71_000
    assert payload["price_breakdown"] == [
        {"quantity": 1, "unit_price": 35_000, "subtotal": 35_000},
        {"quantity": 1, "unit_price": 36_000, "subtotal": 36_000},
    ]


def test_warehouse_api_catalog_and_order_use_owner_seller_price(tmp_path) -> None:
    async def setup_database():
        database_path = (tmp_path / "warehouse-api-seller-price.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            user = User(
                telegram_id=74001,
                full_name="API Seller",
                balance=100_000,
            )
            category = Category(name_vi="Seller", name_en="Seller")
            session.add_all([user, category])
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT API Seller",
                name_en="GPT API Seller",
                price=40_000,
                fulfillment_source="local",
            )
            session.add(product)
            await session.flush()
            session.add_all(
                [
                    SellerPrice(
                        user_id=user.telegram_id,
                        product_id=product.id,
                        profit_per_unit=5_000,
                    ),
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt("api-seller|password"),
                        cost_amount=30_000,
                    ),
                ]
            )
            api_client, api_secret = await ensure_api_client(
                session,
                user.telegram_id,
                cipher,
                60,
            )
            await session.commit()
        return engine, sessions, cipher, product.id, api_client.api_id, api_secret

    engine, sessions, cipher, product_id, api_id, api_secret = asyncio.run(
        setup_database()
    )
    assert api_secret is not None
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=Fernet.generate_key().decode(),
        sepay_enabled=False,
        shop_api_enabled=True,
    )
    app = create_api(
        settings,
        sessions,
        FakeBot(),  # type: ignore[arg-type]
        cipher,
        api_redis=FakeRedis(),  # type: ignore[arg-type]
    )

    with TestClient(app, base_url="https://testserver") as client:
        catalog_path = "/v1/products"
        catalog = client.get(
            catalog_path,
            headers=signed_headers(api_id, api_secret, "GET", catalog_path),
        )
        assert catalog.status_code == 200
        assert catalog.json()["products"][0]["price"] == 35_000

        order_body = json.dumps(
            {"product_id": product_id, "quantity": 1, "max_unit_price": 35_000},
            separators=(",", ":"),
        ).encode()
        order = client.post(
            "/v1/orders",
            content=order_body,
            headers=signed_headers(
                api_id,
                api_secret,
                "POST",
                "/v1/orders",
                order_body,
                idempotency_key="SELLER-PRICE-ORDER-01",
            ),
        )
        assert order.status_code == 200
        assert order.json()["order"]["unit_price"] == 35_000
        assert order.json()["order"]["total_amount"] == 35_000

    async def verify() -> None:
        async with sessions() as session:
            user = await session.get(User, 74001)
            order = await session.scalar(select(Order))
            assert user is not None and user.balance == 65_000
            assert order is not None
            assert order.seller_price_id is not None
            assert order.seller_profit_per_unit == 5_000

    asyncio.run(verify())
    asyncio.run(engine.dispose())


def request_with_headers(headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [
                (key.lower().encode(), value.encode()) for key, value in headers.items()
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 443),
        }
    )


def test_warehouse_api_origin_is_cloudflare_only_and_body_is_bounded() -> None:
    caddyfile = Path("deploy/Caddyfile").read_text(encoding="utf-8")
    token_site = caddyfile.split("token.vietshare.site", 1)[1]
    assert "@direct_origin not remote_ip" in token_site
    assert "respond @direct_origin 403" in token_site
    assert "max_size 8MB" in token_site
    assert "@codex_docs path /codex-api /codex-api/ /codex-api/*" in token_site


def test_codex_guide_and_zip_download_are_public_but_raw_exe_is_not(tmp_path) -> None:
    zip_path = tmp_path / "Custom-Codex-Portable.zip"
    zip_path.write_bytes(b"PK\x03\x04test-archive")
    appimage_path = tmp_path / "Custom-Codex-Ubuntu-x86_64.AppImage"
    appimage_path.write_bytes(b"\x7fELFtest-appimage")
    import_windows_path = tmp_path / "import_to_9router.exe"
    import_windows_path.write_bytes(b"MZtest-importer")
    import_linux_path = tmp_path / "import_to_9router.py"
    import_linux_path.write_text("print('importer')", encoding="utf-8")
    import_windows_zip_path = tmp_path / "import_to_9router_windows.zip"
    import_windows_zip_path.write_bytes(b"PK\x03\x04test-windows-bundle")
    import_linux_zip_path = tmp_path / "import_to_9router_linux.zip"
    import_linux_zip_path.write_bytes(b"PK\x03\x04test-linux-bundle")
    import_macos_zip_path = tmp_path / "import_to_9router_macos.zip"
    import_macos_zip_path.write_bytes(b"PK\x03\x04test-macos-bundle")
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=Fernet.generate_key().decode(),
        sepay_enabled=False,
        shop_api_enabled=True,
        codex_portable_zip_path=str(zip_path),
        codex_ubuntu_appimage_path=str(appimage_path),
        gpt_import_9router_windows_path=str(import_windows_path),
        gpt_import_9router_linux_path=str(import_linux_path),
        gpt_import_9router_windows_zip_path=str(import_windows_zip_path),
        gpt_import_9router_linux_zip_path=str(import_linux_zip_path),
        gpt_import_9router_macos_zip_path=str(import_macos_zip_path),
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app = create_api(
        settings,
        sessions,
        FakeBot(),  # type: ignore[arg-type]
        SecretCipher(settings.inventory_encryption_key.get_secret_value()),
        api_redis=FakeRedis(),  # type: ignore[arg-type]
    )

    with TestClient(app, base_url="https://testserver") as client:
        guide = client.get("/codex-api")
        assert guide.status_code == 200
        assert "https://api.maxdonchal.bond/" not in guide.text
        assert "http://localhost:20128/dashboard/cli-tools/codex" in guide.text
        assert "https://aixingialaire.shop/cdx/v1" in guide.text
        assert "Tạo provider" in guide.text
        assert "Add OpenAI Compatible" in guide.text
        assert "Responses API" in guide.text
        assert "experimental_bearer_token = \"\"" in guide.text
        assert "requires_openai_auth = false" in guide.text
        assert "B1" in guide.text and "B2" in guide.text and "B3" in guide.text
        assert "B4" in guide.text and "B5" in guide.text
        assert "Kết nối" in guide.text and "Codex Desktop" in guide.text and "qua 9Router" in guide.text
        assert "9Router Providers" in guide.text
        assert "Custom Codex" not in guide.text
        assert "chmod +x" not in guide.text
        assert 'href="/codex-api/download"' not in guide.text
        assert 'href="/codex-api/download/ubuntu-x64"' not in guide.text
        assert 'class="sidebar"' in guide.text

        for asset_name in (
            "providers.png",
            "add-provider.png",
            "provider-fields.png",
            "provider-card.png",
            "provider-details.png",
            "api-key.png",
        ):
            asset = client.get(f"/codex-api/assets/{asset_name}")
            assert asset.status_code == 200
            assert asset.headers["content-type"].startswith("image/png")

        assert client.get("/codex-api/assets/../../app.py").status_code == 404

        download = client.get("/codex-api/download")
        assert download.status_code == 200
        assert download.headers["content-type"].startswith("application/zip")
        assert "Custom-Codex-Portable.zip" in download.headers["content-disposition"]
        ubuntu = client.get("/codex-api/download/ubuntu-x64")
        assert ubuntu.status_code == 200
        assert ubuntu.headers["content-type"].startswith("application/vnd.appimage")
        assert "Custom-Codex-Ubuntu-x86_64.AppImage" in ubuntu.headers["content-disposition"]
        import_page = client.get("/codex-api/import-gpt-9router")
        assert import_page.status_code == 200
        assert "Tool Import GPT vào" in import_page.text
        assert "python3 import_to_9router_macos.py" in import_page.text
        windows_tool = client.get("/codex-api/download/import-gpt-9router/windows")
        assert windows_tool.status_code == 200
        assert windows_tool.headers["content-type"].startswith("application/zip")
        assert "import_to_9router_windows.zip" in windows_tool.headers["content-disposition"]
        linux_tool = client.get("/codex-api/download/import-gpt-9router/linux")
        assert linux_tool.status_code == 200
        assert linux_tool.headers["content-type"].startswith("application/zip")
        assert "import_to_9router_linux.zip" in linux_tool.headers["content-disposition"]
        assert 'href="/codex-api/download/import-gpt-9router/macos"' in import_page.text
        assert "macOS" in import_page.text
        macos_tool = client.get("/codex-api/download/import-gpt-9router/macos")
        assert macos_tool.status_code == 200
        assert macos_tool.headers["content-type"].startswith("application/zip")
        assert "import_to_9router_macos.zip" in macos_tool.headers["content-disposition"]
        assert client.get("/codex-api/custom-codex.exe").status_code == 404

    asyncio.run(engine.dispose())


def test_client_ip_trusts_cloudflare_header_only_from_cloudflare() -> None:
    proxied = request_with_headers(
        {
            "X-Forwarded-For": "162.158.114.65",
            "CF-Connecting-IP": "183.81.74.217",
        }
    )
    spoofed = request_with_headers(
        {
            "X-Forwarded-For": "203.0.113.10",
            "CF-Connecting-IP": "198.51.100.20",
        }
    )
    assert client_ip(proxied) == "183.81.74.217"
    assert client_ip(spoofed) == "203.0.113.10"


def signed_headers(
    api_id: str,
    secret: str,
    method: str,
    path: str,
    body: bytes = b"",
    *,
    idempotency_key: str | None = None,
    timestamp_value: int | None = None,
) -> dict[str, str]:
    timestamp = str(timestamp_value if timestamp_value is not None else int(time.time()))
    nonce = secrets.token_hex(12)
    headers = {
        "X-Shop-API-ID": api_id,
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": api_signature(secret, timestamp, nonce, method, path, body),
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    if body:
        headers["Content-Type"] = "application/json"
    return headers


def test_warehouse_api_purchases_from_shared_wallet_and_is_idempotent(tmp_path) -> None:
    async def setup_database():
        database_path = (tmp_path / "warehouse-api.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            referrer = User(
                telegram_id=30001,
                full_name="Referrer",
                balance=0,
                referral_code="REFTEST01",
            )
            buyer = User(
                telegram_id=30002,
                full_name="API buyer",
                balance=50_000,
                referral_code="REFTEST02",
                referred_by_id=referrer.telegram_id,
            )
            category = Category(name_vi="Tài khoản", name_en="Accounts")
            session.add_all([referrer, buyer, category])
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tài khoản API",
                name_en="API account",
                price=20_000,
                allow_quantity=True,
                max_quantity=10,
            )
            session.add(product)
            await session.flush()
            sms_product = Product(
                category_id=category.id,
                name_vi="Thuê số SMS ChatGPT",
                name_en="ChatGPT SMS rental",
                price=2_000,
                product_type="account",
                fulfillment_source="rentsim",
                active=True,
            )
            session.add(sms_product)
            await session.flush()
            haji_product = Product(
                category_id=category.id,
                name_vi="Netflix 4K Premium",
                name_en="Netflix 4K Premium",
                price=25_000,
                product_type="account",
                fulfillment_source="haji",
                supplier_product_id="netflix_4k",
                external_stock=10,
                active=True,
            )
            session.add(haji_product)
            await session.flush()
            session.add_all(
                [
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt(f"api-account-{index}|password"),
                    )
                    for index in (1, 2)
                ]
            )
            api_client, api_secret = await ensure_api_client(
                session,
                buyer.telegram_id,
                cipher,
                60,
            )
            await session.commit()
        return (
            engine,
            sessions,
            cipher,
            product.id,
            sms_product.id,
            haji_product.id,
            api_client.api_id,
            api_secret,
        )

    (
        engine,
        sessions,
        cipher,
        product_id,
        sms_product_id,
        haji_product_id,
        api_id,
        api_secret,
    ) = asyncio.run(setup_database())
    assert api_secret is not None
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=Fernet.generate_key().decode(),
        sepay_enabled=False,
        shop_api_enabled=True,
        referral_commission_percent=5,
    )
    app = create_api(
        settings,
        sessions,
        FakeBot(),  # type: ignore[arg-type]
        cipher,
        api_redis=FakeRedis(),  # type: ignore[arg-type]
    )

    body = json.dumps(
        {"product_id": product_id, "quantity": 1, "max_unit_price": 20_000},
        separators=(",", ":"),
    ).encode()
    with TestClient(app, base_url="https://testserver") as client:
        docs = client.get("/docs")
        assert docs.status_code == 200
        assert "Tài liệu API đấu kho" in docs.text
        assert "HMAC-SHA256" in docs.text
        assert "POST /v1/orders" in docs.text
        assert "Idempotency-Key" in docs.text
        assert "https://token.vietshare.site/v1" in docs.text
        assert "Tạo QR nạp ví" not in docs.text
        assert "flash_sale_id" in docs.text
        assert "max_unit_price" in docs.text
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert "/v1/warehouse/inventory/import" not in docs.text

        assert client.get("/codex-claude").status_code == 404
        assert client.get("/codex-setup.zip").status_code == 404
        assert client.get("/codex-setup.exe").status_code == 404

        docs_redirect = client.get("/v1/docs", follow_redirects=False)
        assert docs_redirect.status_code == 307
        assert docs_redirect.headers["location"] == "/docs"

        information = client.get("/v1")
        assert information.status_code == 200
        assert information.headers["cache-control"] == "no-store"
        assert information.json()["documentation"] == "https://token.vietshare.site/docs"
        assert all("deposits" not in endpoint for endpoint in information.json()["endpoints"])

        removed_deposit_endpoint = client.post("/v1/deposits", json={"amount": 100_000})
        assert removed_deposit_endpoint.status_code == 404

        products = client.get(
            "/v1/catalog",
            headers=signed_headers(api_id, api_secret, "GET", "/v1/catalog"),
        )
        assert products.status_code == 200
        assert products.headers["cache-control"] == "no-store"
        assert products.json()["count"] == 1
        assert products.json()["products"][0]["stock"] == 2
        assert products.json()["products"][0]["flash_sale_id"] is None

        for path in (
            f"/v1/products/{sms_product_id}",
            f"/v1/stock/{sms_product_id}",
            f"/v1/products/{haji_product_id}",
            f"/v1/stock/{haji_product_id}",
        ):
            blocked_stock = client.get(
                path,
                headers=signed_headers(api_id, api_secret, "GET", path),
            )
            assert blocked_stock.status_code == 404
            assert blocked_stock.json()["detail"]["code"] == "PRODUCT_NOT_FOUND"

        blocked_body = json.dumps(
            {"product_id": sms_product_id, "quantity": 1, "max_unit_price": 2_000},
            separators=(",", ":"),
        ).encode()
        blocked_order = client.post(
            "/v1/orders",
            content=blocked_body,
            headers=signed_headers(
                api_id,
                api_secret,
                "POST",
                "/v1/orders",
                blocked_body,
                idempotency_key="SMS-ORDER-BLOCKED-01",
            ),
        )
        assert blocked_order.status_code == 404
        assert blocked_order.json()["detail"]["code"] == "PRODUCT_NOT_FOUND"

        haji_blocked_body = json.dumps(
            {"product_id": haji_product_id, "quantity": 1, "max_unit_price": 25_000},
            separators=(",", ":"),
        ).encode()
        haji_blocked_order = client.post(
            "/v1/orders",
            content=haji_blocked_body,
            headers=signed_headers(
                api_id,
                api_secret,
                "POST",
                "/v1/orders",
                haji_blocked_body,
                idempotency_key="HAJI-ORDER-BLOCKED-01",
            ),
        )
        assert haji_blocked_order.status_code == 404
        assert haji_blocked_order.json()["detail"]["code"] == "PRODUCT_NOT_FOUND"

        missing_price_body = json.dumps(
            {"product_id": product_id, "quantity": 1},
            separators=(",", ":"),
        ).encode()
        missing_price = client.post(
            "/v1/orders",
            content=missing_price_body,
            headers=signed_headers(
                api_id,
                api_secret,
                "POST",
                "/v1/orders",
                missing_price_body,
                idempotency_key="ORDER-MISSING-PRICE",
            ),
        )
        assert missing_price.status_code == 400
        assert missing_price.json()["detail"]["code"] == "MAX_UNIT_PRICE_REQUIRED"

        stale_price_body = json.dumps(
            {"product_id": product_id, "quantity": 1, "max_unit_price": 19_000},
            separators=(",", ":"),
        ).encode()
        stale_price = client.post(
            "/v1/orders",
            content=stale_price_body,
            headers=signed_headers(
                api_id,
                api_secret,
                "POST",
                "/v1/orders",
                stale_price_body,
                idempotency_key="ORDER-STALE-PRICE",
            ),
        )
        assert stale_price.status_code == 409
        assert stale_price.json()["detail"]["code"] == "PRICE_CHANGED"

        first = client.post(
            "/v1/orders",
            content=body,
            headers=signed_headers(
                api_id,
                api_secret,
                "POST",
                "/v1/orders",
                body,
                idempotency_key="ORDER-CLIENT-0001",
            ),
        )
        assert first.status_code == 200
        order = first.json()["order"]
        assert order["channel"] == "api"
        assert order["unit_price"] == 20_000
        assert order["total_amount"] == 20_000
        assert order["accounts"] == ["api-account-1|password"]
        assert order["idempotency_key"] == "ORDER-CLIENT-0001"

        repeated = client.post(
            "/v1/orders",
            content=body,
            headers=signed_headers(
                api_id,
                api_secret,
                "POST",
                "/v1/orders",
                body,
                idempotency_key="ORDER-CLIENT-0001",
            ),
        )
        assert repeated.status_code == 200
        assert repeated.json()["order"]["order_code"] == order["order_code"]

        changed_body = json.dumps(
            {"product_id": product_id, "quantity": 2, "max_unit_price": 20_000},
            separators=(",", ":"),
        ).encode()
        mismatch = client.post(
            "/v1/orders",
            content=changed_body,
            headers=signed_headers(
                api_id,
                api_secret,
                "POST",
                "/v1/orders",
                changed_body,
                idempotency_key="ORDER-CLIENT-0001",
            ),
        )
        assert mismatch.status_code == 409
        assert mismatch.json()["detail"]["code"] == "IDEMPOTENCY_MISMATCH"

        async def pause_product() -> None:
            async with sessions() as session:
                product = await session.get(Product, product_id)
                assert product is not None
                product.force_out_of_stock = True
                await session.commit()

        asyncio.run(pause_product())
        paused_catalog = client.get(
            "/v1/catalog",
            headers=signed_headers(api_id, api_secret, "GET", "/v1/catalog"),
        )
        assert paused_catalog.status_code == 200
        assert paused_catalog.json()["products"][0]["stock"] == 0

    async def verify_database() -> None:
        async with sessions() as session:
            buyer = await session.get(User, 30002)
            referrer = await session.get(User, 30001)
            orders = list(await session.scalars(select(Order)))
            rewards = list(await session.scalars(select(ReferralReward)))
            requests = list(await session.scalars(select(ApiOrderRequest)))
            audits = list(await session.scalars(select(ApiRequestAudit)))
            assert buyer is not None and buyer.balance == 30_000
            assert referrer is not None and referrer.balance == 1_000
            assert len(orders) == 1 and orders[0].sales_channel == "api"
            assert len(rewards) == 1 and rewards[0].commission_amount == 1_000
            assert sorted(request.status for request in requests) == ["completed", "failed"]
            assert audits and all(audit.api_client_id is not None for audit in audits)
            assert int(await session.scalar(select(func.count(InventoryItem.id))) or 0) == 2
        await engine.dispose()

    asyncio.run(verify_database())


def test_public_api_keeps_supplier_failure_in_review_and_retries_same_key(
    tmp_path,
    monkeypatch,
) -> None:
    async def setup_database():
        database_path = (tmp_path / "api-review.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            user = User(telegram_id=31001, full_name="API review user", balance=50_000)
            category = Category(name_vi="Accounts", name_en="Accounts")
            session.add_all([user, category])
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Le Hai product",
                name_en="Le Hai product",
                price=32_000,
                fulfillment_source="lehai",
                supplier_product_id="cdk_ggpro_18m",
                active=True,
            )
            session.add(product)
            await session.flush()
            campaign = FlashSaleCampaign(
                product_id=product.id,
                original_price=32_000,
                sale_price=30_000,
                total_quantity=5,
                message_text="API sale",
            )
            session.add(campaign)
            await session.flush()
            api_client, api_secret = await ensure_api_client(
                session,
                user.telegram_id,
                cipher,
                60,
            )
            await session.commit()
        return (
            engine,
            sessions,
            cipher,
            product.id,
            campaign.id,
            api_client.api_id,
            api_secret,
        )

    engine, sessions, cipher, product_id, campaign_id, api_id, api_secret = asyncio.run(
        setup_database()
    )
    calls: list[tuple[str | None, int | None]] = []

    async def fake_purchase(*_args, **kwargs):
        calls.append(
            (
                kwargs.get("supplier_idempotency_key"),
                kwargs.get("expected_flash_sale_id"),
            )
        )
        return PurchaseResult(False, "supplier_unavailable")

    monkeypatch.setattr("app.public_api.purchase_product", fake_purchase)
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=Fernet.generate_key().decode(),
        sepay_enabled=False,
        shop_api_enabled=True,
    )
    app = create_api(
        settings,
        sessions,
        FakeBot(),  # type: ignore[arg-type]
        cipher,
        api_redis=FakeRedis(),  # type: ignore[arg-type]
    )
    body = json.dumps(
        {"product_id": product_id, "max_unit_price": 30_000},
        separators=(",", ":"),
    ).encode()
    with TestClient(app, base_url="https://testserver") as client:
        first = client.post(
            "/v1/orders",
            content=body,
            headers=signed_headers(
                api_id,
                api_secret,
                "POST",
                "/v1/orders",
                body,
                idempotency_key="REVIEW-ORDER-001",
            ),
        )
        assert first.status_code == 202
        assert first.json()["status"] == "review"
        assert first.headers["retry-after"] == "10"

        second = client.post(
            "/v1/orders",
            content=body,
            headers=signed_headers(
                api_id,
                api_secret,
                "POST",
                "/v1/orders",
                body,
                idempotency_key="REVIEW-ORDER-001",
            ),
        )
        assert second.status_code == 202
        assert second.json()["status"] == "review"

    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[0][0] is not None and calls[0][0].startswith("shop-api-")
    assert calls[0][1] == campaign_id

    async def verify_database() -> None:
        async with sessions() as session:
            request = await session.scalar(select(ApiOrderRequest))
            assert request is not None and request.status == "review"
        await engine.dispose()

    asyncio.run(verify_database())


def test_rotated_secret_immediately_invalidates_old_secret(tmp_path) -> None:
    async def setup_database():
        database_path = (tmp_path / "api-rotation.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            user = User(telegram_id=40001, full_name="Partner", balance=0)
            session.add(user)
            await session.flush()
            api_client, old_secret = await ensure_api_client(session, user.telegram_id, cipher, 60)
            _, new_secret = await rotate_api_secret(session, user.telegram_id, cipher)
            await session.commit()
        return engine, sessions, cipher, api_client.api_id, old_secret, new_secret

    engine, sessions, cipher, api_id, old_secret, new_secret = asyncio.run(setup_database())
    assert old_secret is not None
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=Fernet.generate_key().decode(),
        sepay_enabled=False,
    )
    app = create_api(
        settings,
        sessions,
        FakeBot(),  # type: ignore[arg-type]
        cipher,
        api_redis=FakeRedis(),  # type: ignore[arg-type]
    )
    with TestClient(app, base_url="https://testserver") as client:
        rejected = client.get(
            "/v1/account",
            headers=signed_headers(api_id, old_secret, "GET", "/v1/account"),
        )
        accepted = client.get(
            "/v1/account",
            headers=signed_headers(api_id, new_secret, "GET", "/v1/account"),
        )
        assert rejected.status_code == 401
        assert accepted.status_code == 200
    asyncio.run(engine.dispose())


def test_public_api_rate_limit_uses_server_time_bucket(tmp_path) -> None:
    async def setup_database():
        database_path = (tmp_path / "api-rate-limit.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            user = User(telegram_id=41001, full_name="Rate limited partner")
            session.add(user)
            await session.flush()
            api_client, api_secret = await ensure_api_client(
                session,
                user.telegram_id,
                cipher,
                1,
            )
            await session.commit()
        return engine, sessions, cipher, api_client.api_id, api_secret

    engine, sessions, cipher, api_id, api_secret = asyncio.run(setup_database())
    assert api_secret is not None
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=Fernet.generate_key().decode(),
        sepay_enabled=False,
        shop_api_enabled=True,
    )
    app = create_api(
        settings,
        sessions,
        FakeBot(),  # type: ignore[arg-type]
        cipher,
        api_redis=FakeRedis(),  # type: ignore[arg-type]
    )
    current = int(time.time())
    with TestClient(app, base_url="https://testserver") as client:
        first = client.get(
            "/v1/account",
            headers=signed_headers(
                api_id,
                api_secret,
                "GET",
                "/v1/account",
                timestamp_value=current,
            ),
        )
        second = client.get(
            "/v1/account",
            headers=signed_headers(
                api_id,
                api_secret,
                "GET",
                "/v1/account",
                timestamp_value=current - 60,
            ),
        )
        assert first.status_code == 200
        assert second.status_code == 429
        assert second.json()["detail"]["code"] == "RATE_LIMITED"
    asyncio.run(engine.dispose())


def test_stale_processing_idempotency_request_can_be_retried(tmp_path) -> None:
    async def setup_database():
        database_path = (tmp_path / "api-stale-request.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            user = User(telegram_id=42001, full_name="Stale request buyer", balance=20_000)
            category = Category(name_vi="API", name_en="API")
            session.add_all([user, category])
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Stale request product",
                name_en="Stale request product",
                price=10_000,
                allow_quantity=True,
                max_quantity=10,
            )
            session.add(product)
            await session.flush()
            session.add(
                InventoryItem(
                    product_id=product.id,
                    encrypted_secret=cipher.encrypt("stale-account|password"),
                )
            )
            api_client, api_secret = await ensure_api_client(
                session,
                user.telegram_id,
                cipher,
                60,
            )
            request_hash = hashlib.sha256(
                json.dumps(
                    {
                        "coupon_code": None,
                        "flash_sale_id": None,
                        "max_unit_price": 10_000,
                        "product_id": product.id,
                        "quantity": 1,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            old = datetime.now(UTC) - timedelta(hours=1)
            session.add(
                ApiOrderRequest(
                    api_client_id=api_client.id,
                    idempotency_key="STALE-REQ-001",
                    request_hash=request_hash,
                    status="processing",
                    created_at=old,
                    updated_at=old,
                )
            )
            await session.commit()
        return engine, sessions, cipher, product.id, api_client.api_id, api_secret

    engine, sessions, cipher, product_id, api_id, api_secret = asyncio.run(setup_database())
    assert api_secret is not None
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=Fernet.generate_key().decode(),
        sepay_enabled=False,
        shop_api_enabled=True,
    )
    app = create_api(
        settings,
        sessions,
        FakeBot(),  # type: ignore[arg-type]
        cipher,
        api_redis=FakeRedis(),  # type: ignore[arg-type]
    )
    body = json.dumps(
        {"product_id": product_id, "quantity": 1, "max_unit_price": 10_000},
        separators=(",", ":"),
    ).encode()
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/v1/orders",
            content=body,
            headers=signed_headers(
                api_id,
                api_secret,
                "POST",
                "/v1/orders",
                body,
                idempotency_key="STALE-REQ-001",
            ),
        )
        assert response.status_code == 200
        assert response.json()["order"]["accounts"] == ["stale-account|password"]
    asyncio.run(engine.dispose())
