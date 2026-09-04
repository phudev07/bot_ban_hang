import asyncio

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import create_api
from app.codex_quota import (
    CodexQuotaError,
    fetch_codex_quota,
    parse_codex_quota,
    quota_profile_url,
    validate_codex_key,
)
from app.config import Settings
from app.database import Base
from app.utils import SecretCipher


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, int | str] = {}

    async def incr(self, key: str) -> int:
        value = int(self.values.get(key, 0)) + 1
        self.values[key] = value
        return value

    async def expire(self, _key: str, _seconds: int) -> bool:
        return True

    async def aclose(self) -> None:
        return None


def test_quota_profile_url_matches_cockpit_tools_path() -> None:
    assert (
        quota_profile_url("https://aixingialaire.shop/cdx/v1")
        == "https://aixingialaire.shop/api/cockpit-tools/token-profile"
    )


def test_parse_codex_quota_formats_100m_remaining() -> None:
    quota = parse_codex_quota(
        {
            "success": True,
            "data": {
                "usage": {
                    "total_granted": 100_000_000,
                    "total_used": 0,
                    "total_available": 100_000_000,
                    "unlimited_quota": False,
                }
            },
        }
    )
    assert quota.display == "100M / 100M"
    assert quota.remaining == 100_000_000
    assert quota.percentage == 100


def test_fetch_codex_quota_uses_bearer_without_persisting_key() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["authorization"]
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "usage": {
                        "total_granted": "100000000",
                        "total_used": "2500000",
                        "total_available": "97500000",
                        "expires_at": 1_800_000_000,
                    }
                },
            },
        )

    async def run() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = await fetch_codex_quota(
                "sk-cdx-test-key",
                base_url="https://aixingialaire.shop/cdx/v1",
                client=client,
            )
            assert result.remaining == 97_500_000
        finally:
            await client.aclose()

    asyncio.run(run())
    assert seen["url"] == "https://aixingialaire.shop/api/cockpit-tools/token-profile"
    assert seen["authorization"] == "Bearer sk-cdx-test-key"


def test_invalid_key_and_upstream_errors_are_sanitized() -> None:
    try:
        parse_codex_quota({"success": True, "data": {}})
    except CodexQuotaError as exc:
        assert exc.code == "INVALID_RESPONSE"
    else:
        raise AssertionError("expected malformed response error")

    async def run() -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(401, text="secret upstream detail"))
        )
        try:
            try:
                await fetch_codex_quota("sk-cdx-test-key", base_url="https://example.com/v1", client=client)
            except CodexQuotaError as exc:
                assert exc.code == "INVALID_KEY"
                assert "secret upstream" not in exc.message
            else:
                raise AssertionError("expected invalid key error")
        finally:
            await client.aclose()

    asyncio.run(run())


def test_public_quota_page_and_check_endpoint(monkeypatch) -> None:
    from app import public_api

    async def fake_fetch(_key: str, *, base_url: str):
        validate_codex_key(_key)
        assert base_url == "https://aixingialaire.shop/cdx/v1"
        return parse_codex_quota(
            {"data": {"usage": {"total_granted": 100_000_000, "total_available": 100_000_000}}}
        )

    monkeypatch.setattr(public_api, "fetch_codex_quota", fake_fetch)
    async def setup():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        return engine, async_sessionmaker(engine, expire_on_commit=False)

    engine, sessions = asyncio.run(setup())
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
        object(),  # type: ignore[arg-type]
        SecretCipher(settings.inventory_encryption_key.get_secret_value()),
        api_redis=FakeRedis(),  # type: ignore[arg-type]
    )
    with TestClient(app) as client:
        page = client.get("/codex-api/quota")
        assert page.status_code == 200
        assert "Làm mới hạn mức" in page.text
        checked = client.post("/codex-api/quota/check", json={"key": "sk-cdx-demo"})
        assert checked.status_code == 200
        assert checked.json()["quota"]["display"] == "100M / 100M"
        invalid = client.post("/codex-api/quota/check", json={"key": "not-a-codex-key"})
        assert invalid.status_code == 400
        assert "sk-cdx" in invalid.json()["message"]
    asyncio.run(engine.dispose())
