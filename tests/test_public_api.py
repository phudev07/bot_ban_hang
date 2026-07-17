import asyncio
import json
import secrets
import time

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import create_api
from app.config import Settings
from app.database import Base
from app.models import ApiOrderRequest, Category, InventoryItem, Order, Product, ReferralReward, User
from app.partner_services import api_signature, ensure_api_client, rotate_api_secret
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

        docs_redirect = client.get("/v1/docs", follow_redirects=False)
        assert docs_redirect.status_code == 307
        assert docs_redirect.headers["location"] == "/docs"

        information = client.get("/v1")
        assert information.status_code == 200
        assert information.json()["documentation"] == "https://token.vietshare.site/docs"

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
