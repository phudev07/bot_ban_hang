import asyncio
import json
import secrets
import time
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import create_api
from app.config import Settings
from app.database import Base
from app.models import Category, InventoryItem, Product, WarehouseImportRequest
from app.partner_services import api_signature
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
    secret: str,
    method: str,
    path: str,
    body: bytes,
    *,
    nonce: str | None = None,
    idempotency_key: str = "AUTO-IMPORT-0001",
):
    timestamp = str(int(time.time()))
    nonce = nonce or secrets.token_hex(12)
    return {
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": api_signature(secret, timestamp, nonce, method, path, body),
        "Idempotency-Key": idempotency_key,
        "Content-Type": "application/json",
    }


def test_warehouse_import_is_authenticated_idempotent_and_deduplicated(tmp_path: Path) -> None:
    async def setup_database():
        engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'warehouse.db').as_posix()}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="Tài khoản", name_en="Accounts")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus",
                name_en="GPT Plus",
                price=40_000,
                fulfillment_source="local",
                active=True,
            )
            session.add(product)
            await session.commit()
            return engine, sessions, cipher, product.id

    engine, sessions, cipher, product_id = asyncio.run(setup_database())
    secret = "warehouse-test-secret"
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=Fernet.generate_key().decode(),
        shop_api_enabled=False,
        warehouse_api_enabled=True,
        warehouse_api_key=secret,
    )
    app = create_api(
        settings,
        sessions,
        FakeBot(),  # type: ignore[arg-type]
        cipher,
        api_redis=FakeRedis(),  # type: ignore[arg-type]
    )
    path = "/v1/warehouse/inventory/import"
    payload = {
        "product_id": product_id,
        "items": ["one@example.com|pw1", "one@example.com|pw2", "two@example.com|pw3"],
        "cost_amount": "35.000",
        "new_import_note": "tool tự động",
        "notify_stock_arrival": True,
    }
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()

    with TestClient(app, base_url="https://testserver") as client:
        stock_path = f"/v1/warehouse/inventory/stock?product_id={product_id}"
        stock_body = b""
        stock = client.get(
            stock_path,
            headers=signed_headers(secret, "GET", stock_path, stock_body),
        )
        assert stock.status_code == 200
        assert stock.json()["local_stock"] == 0
        assert stock.json()["source_stock"] == 0
        assert stock.json()["total_stock"] == 0
        assert stock.json()["has_stock"] is False

        response = client.post(path, content=body, headers=signed_headers(secret, "POST", path, body))
        assert response.status_code == 200
        assert response.json()["accepted_count"] == 2
        assert response.json()["duplicate_count"] == 1

        stock_after = client.get(
            stock_path,
            headers=signed_headers(secret, "GET", stock_path, stock_body),
        )
        assert stock_after.status_code == 200
        assert stock_after.json()["local_stock"] == 2
        assert stock_after.json()["total_stock"] == 2
        assert stock_after.json()["has_stock"] is True

        replay_body = body
        replay = client.post(
            path,
            content=replay_body,
            headers=signed_headers(secret, "POST", path, replay_body),
        )
        assert replay.status_code == 200
        assert replay.json() == response.json()

        mismatch_payload = {**payload, "items": ["different@example.com|pw"]}
        mismatch_body = json.dumps(mismatch_payload, separators=(",", ":"), ensure_ascii=False).encode()
        mismatch = client.post(
            path,
            content=mismatch_body,
            headers=signed_headers(secret, "POST", path, mismatch_body),
        )
        assert mismatch.status_code == 409
        assert mismatch.json()["detail"]["code"] == "IDEMPOTENCY_MISMATCH"

        replay_nonce = secrets.token_hex(12)
        replay_headers = signed_headers(
            secret,
            "POST",
            path,
            body,
            nonce=replay_nonce,
            idempotency_key="AUTO-IMPORT-REPLAY",
        )
        assert client.post(path, content=body, headers=replay_headers).status_code == 200
        assert client.post(path, content=body, headers=replay_headers).status_code == 409

    async def verify_database():
        async with sessions() as session:
            items = list(await session.scalars(InventoryItem.__table__.select()))
            requests = list(await session.scalars(WarehouseImportRequest.__table__.select()))
            return len(items), len(requests)

    item_count, request_count = asyncio.run(verify_database())
    assert item_count == 2
    assert request_count == 2
    asyncio.run(engine.dispose())
