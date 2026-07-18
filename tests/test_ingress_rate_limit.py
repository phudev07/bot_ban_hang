import asyncio
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.api as api_module
from app.api import create_api
from app.config import Settings
from app.database import Base
from app.services import PaymentResult
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


class ProgressBot:
    def __init__(self) -> None:
        self.events: list[tuple[str, int, object]] = []

    async def send_message(self, chat_id: int, text: str, **_kwargs):
        message_id = len(self.events) + 1
        self.events.append(("send", chat_id, text))
        return SimpleNamespace(message_id=message_id)

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.events.append(("delete", chat_id, message_id))


def test_public_api_and_sepay_have_pre_authentication_limits(tmp_path) -> None:
    async def setup_database():
        database_path = (tmp_path / "ingress-rate.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        return engine, async_sessionmaker(engine, expire_on_commit=False)

    engine, sessions = asyncio.run(setup_database())
    encryption_key = Fernet.generate_key().decode()
    config = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=encryption_key,
        sepay_enabled=True,
        sepay_auth_mode="api_key",
        sepay_api_key="rate-test-key",
        bank_code="TEST",
        bank_account="0000000000",
        bank_account_name="RATE TEST",
        public_api_ip_rate_limit_per_minute=10,
        public_api_global_rate_limit_per_minute=10,
        sepay_webhook_rate_limit_per_minute=10,
        sepay_webhook_global_rate_limit_per_minute=10,
    )
    app = create_api(
        config,
        sessions,
        FakeBot(),  # type: ignore[arg-type]
        SecretCipher(encryption_key),
        api_redis=FakeRedis(),  # type: ignore[arg-type]
    )

    with TestClient(app, base_url="https://testserver") as client:
        api_responses = [client.get("/v1/account") for _ in range(11)]
        assert [response.status_code for response in api_responses[:10]] == [401] * 10
        assert api_responses[10].status_code == 429
        assert api_responses[10].headers["retry-after"]

        webhook_responses = [
            client.post("/webhooks/sepay", headers={"Authorization": "Apikey invalid"})
            for _ in range(11)
        ]
        assert [response.status_code for response in webhook_responses[:10]] == [401] * 10
        assert webhook_responses[10].status_code == 429
        assert webhook_responses[10].json()["detail"]["code"] == "RATE_LIMITED"

    asyncio.run(engine.dispose())


def test_direct_payment_progress_is_deleted_before_final_status(tmp_path, monkeypatch) -> None:
    async def setup_database():
        database_path = (tmp_path / "payment-progress.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        return engine, async_sessionmaker(engine, expire_on_commit=False)

    async def fake_process_payment(*args, **_kwargs) -> PaymentResult:
        progress_callback = args[-1]
        await progress_callback(123456, "vi")
        return PaymentResult(
            "direct_purchase_fallback",
            user_id=123456,
            amount=20_000,
            language="vi",
            balance=20_000,
        )

    monkeypatch.setattr(api_module, "process_sepay_payment", fake_process_payment)
    engine, sessions = asyncio.run(setup_database())
    encryption_key = Fernet.generate_key().decode()
    config = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=encryption_key,
        sepay_enabled=True,
        sepay_auth_mode="api_key",
        sepay_api_key="progress-test-key",
        bank_code="TEST",
        bank_account="0000000000",
        bank_account_name="PROGRESS TEST",
    )
    bot = ProgressBot()
    app = create_api(
        config,
        sessions,
        bot,  # type: ignore[arg-type]
        SecretCipher(encryption_key),
        api_redis=FakeRedis(),  # type: ignore[arg-type]
    )

    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/webhooks/sepay",
            headers={"Authorization": "Apikey progress-test-key"},
            json={"id": 1, "transferType": "in", "transferAmount": 20_000},
        )

    assert response.status_code == 200
    assert bot.events[0][0] == "send"
    assert "Đang lấy hàng" in str(bot.events[0][2])
    assert bot.events[1] == ("delete", 123456, 1)
    assert bot.events[2][0] == "send"
    assert "cộng vào số dư" in str(bot.events[2][2])
    asyncio.run(engine.dispose())
