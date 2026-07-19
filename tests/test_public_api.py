import asyncio
import json
import secrets
import time

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.api import create_api
from app.config import Settings
from app.database import Base
from app.models import ApiOrderRequest, Category, InventoryItem, Order, Product, ReferralReward, User
from app.partner_services import api_signature, ensure_api_client, rotate_api_secret
from app.services import PurchaseResult
from app.public_api import client_ip
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
) -> dict[str, str]:
    timestamp = str(int(time.time()))
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
        return engine, sessions, cipher, product.id, api_client.api_id, api_secret

    engine, sessions, cipher, product_id, api_id, api_secret = asyncio.run(setup_database())
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
        {"product_id": product_id, "quantity": 1},
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

        docs_redirect = client.get("/v1/docs", follow_redirects=False)
        assert docs_redirect.status_code == 307
        assert docs_redirect.headers["location"] == "/docs"

        information = client.get("/v1")
        assert information.status_code == 200
        assert information.json()["documentation"] == "https://token.vietshare.site/docs"
        assert all("deposits" not in endpoint for endpoint in information.json()["endpoints"])

        removed_deposit_endpoint = client.post("/v1/deposits", json={"amount": 100_000})
        assert removed_deposit_endpoint.status_code == 404

        products = client.get(
            "/v1/catalog",
            headers=signed_headers(api_id, api_secret, "GET", "/v1/catalog"),
        )
        assert products.status_code == 200
        assert products.json()["products"][0]["stock"] == 2

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
        assert order["total_amount"] == 20_000
        assert order["accounts"] == ["api-account-1|password"]

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
            {"product_id": product_id, "quantity": 2},
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

    async def verify_database() -> None:
        async with sessions() as session:
            buyer = await session.get(User, 30002)
            referrer = await session.get(User, 30001)
            orders = list(await session.scalars(select(Order)))
            rewards = list(await session.scalars(select(ReferralReward)))
            requests = list(await session.scalars(select(ApiOrderRequest)))
            assert buyer is not None and buyer.balance == 30_000
            assert referrer is not None and referrer.balance == 1_000
            assert len(orders) == 1 and orders[0].sales_channel == "api"
            assert len(rewards) == 1 and rewards[0].commission_amount == 1_000
            assert len(requests) == 1 and requests[0].status == "completed"
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
            api_client, api_secret = await ensure_api_client(
                session,
                user.telegram_id,
                cipher,
                60,
            )
            await session.commit()
        return engine, sessions, cipher, product.id, api_client.api_id, api_secret

    engine, sessions, cipher, product_id, api_id, api_secret = asyncio.run(setup_database())
    calls: list[str | None] = []

    async def fake_purchase(*_args, **kwargs):
        calls.append(kwargs.get("supplier_idempotency_key"))
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
    body = json.dumps({"product_id": product_id}, separators=(",", ":")).encode()
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
    assert calls[0] is not None and calls[0].startswith("shop-api-")

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
