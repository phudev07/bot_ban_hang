import asyncio
import json
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.router_tokens import (
    RouterTokenClient,
    RouterTokenUsage,
    RouterUsageLog,
    RouterUsageModel,
)
from app.token_usage import UsageRateLimiter, create_token_usage_router


def usage_payload() -> RouterTokenUsage:
    return RouterTokenUsage(
        shop_order_id="RT-CYBER-001",
        key_id="key-001",
        token_quota=10_000_000,
        tokens_used=3_000_000,
        reserved_tokens=0,
        remaining_tokens=7_000_000,
        available_tokens=7_000_000,
        is_active=True,
        disabled_reason=None,
        created_at="2026-07-17T10:00:00Z",
        updated_at="2026-07-17T11:00:00Z",
        total_requests=2,
        input_tokens=2_000_000,
        output_tokens=1_000_000,
        total_log_tokens=3_000_000,
        first_request_at="2026-07-17T10:10:00Z",
        last_request_at="2026-07-17T10:20:00Z",
        models=(
            RouterUsageModel(
                model="gpt-5",
                requests=2,
                input_tokens=2_000_000,
                output_tokens=1_000_000,
                total_tokens=3_000_000,
            ),
        ),
        logs=(
            RouterUsageLog(
                timestamp="2026-07-17T10:20:00Z",
                model="gpt-5",
                input_tokens=1_200_000,
                output_tokens=500_000,
                total_tokens=1_700_000,
                status="ok",
            ),
        ),
    )


def test_public_usage_page_and_lookup_do_not_echo_api_key() -> None:
    class FakeClient:
        async def usage(self, api_key: str) -> RouterTokenUsage:
            assert api_key == "sk-cyber-test-key"
            return usage_payload()

    app = FastAPI()
    app.include_router(create_token_usage_router(FakeClient()))  # type: ignore[arg-type]

    with TestClient(app, base_url="https://testserver") as client:
        page = client.get("/token-usage")
        assert page.status_code == 200
        assert "Kiểm soát từng" in page.text
        assert "Nhật ký request gần nhất" in page.text
        assert 'name="robots" content="noindex,nofollow,noarchive"' in page.text
        assert page.headers["cache-control"].startswith("no-store")
        assert page.headers["x-robots-tag"] == "noindex, nofollow, noarchive"

        result = client.post(
            "/api/token-usage",
            json={"api_key": "sk-cyber-test-key"},
        )
        assert result.status_code == 200
        assert result.json()["usage"]["remaining_tokens"] == 7_000_000
        assert result.json()["usage"]["logs"][0]["input_tokens"] == 1_200_000
        assert "sk-cyber-test-key" not in result.text
        assert result.headers["referrer-policy"] == "no-referrer"


def test_usage_lookup_rate_limits_repeated_key_probes() -> None:
    class FakeClient:
        async def usage(self, _api_key: str) -> RouterTokenUsage:
            return usage_payload()

    app = FastAPI()
    app.include_router(
        create_token_usage_router(
            FakeClient(),  # type: ignore[arg-type]
            limiter=UsageRateLimiter(limit=1, window_seconds=60),
        )
    )

    with TestClient(app, base_url="https://testserver") as client:
        first = client.post("/api/token-usage", json={"api_key": "invalid"})
        blocked = client.post("/api/token-usage", json={"api_key": "invalid-again"})

    assert first.status_code == 422
    assert blocked.status_code == 429


def test_cloudflare_client_ip_has_independent_rate_limit_bucket() -> None:
    class FakeClient:
        async def usage(self, _api_key: str) -> RouterTokenUsage:
            return usage_payload()

    app = FastAPI()
    app.include_router(
        create_token_usage_router(
            FakeClient(),  # type: ignore[arg-type]
            limiter=UsageRateLimiter(limit=1, window_seconds=60),
        )
    )

    with TestClient(app, base_url="https://testserver") as client:
        first_ip = client.post(
            "/api/token-usage",
            json={"api_key": "invalid"},
            headers={"cf-connecting-ip": "203.0.113.10"},
        )
        second_ip = client.post(
            "/api/token-usage",
            json={"api_key": "invalid"},
            headers={"cf-connecting-ip": "203.0.113.11"},
        )

    assert first_ip.status_code == 422
    assert second_ip.status_code == 422


def test_router_client_reads_per_key_usage() -> None:
    async def scenario() -> None:
        settings = SimpleNamespace(
            router_base_url="https://router.example.com",
            router_hmac_secret=SecretStr("shop-secret"),
            router_public_api_url="https://router.example.com/v1",
            public_base_url="https://shop.example.com",
            token_usage_url="https://token.example.com/token-usage",
            router_allowed_models=("gpt-*",),
            router_timeout_seconds=5,
        )
        client = RouterTokenClient(settings)  # type: ignore[arg-type]
        await client.client.aclose()

        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/internal/shop/usage"
            assert json.loads(request.content) == {"apiKey": "sk-client-usage-test"}
            assert request.headers["x-shop-signature"]
            return httpx.Response(
                200,
                json={
                    "shopOrderId": "RT-CYBER-001",
                    "keyId": "key-001",
                    "tokenQuota": 10_000_000,
                    "tokensUsed": 3_000_000,
                    "reservedTokens": 0,
                    "remainingTokens": 7_000_000,
                    "availableTokens": 7_000_000,
                    "isActive": True,
                    "createdAt": "2026-07-17T10:00:00Z",
                    "updatedAt": "2026-07-17T11:00:00Z",
                    "stats": {
                        "totalRequests": 2,
                        "inputTokens": 2_000_000,
                        "outputTokens": 1_000_000,
                        "totalTokens": 3_000_000,
                        "lastRequestAt": "2026-07-17T10:20:00Z",
                        "models": [
                            {
                                "model": "gpt-5",
                                "requests": 2,
                                "inputTokens": 2_000_000,
                                "outputTokens": 1_000_000,
                                "totalTokens": 3_000_000,
                            }
                        ],
                    },
                    "logs": [
                        {
                            "timestamp": "2026-07-17T10:20:00Z",
                            "model": "gpt-5",
                            "inputTokens": 1_200_000,
                            "outputTokens": 500_000,
                            "totalTokens": 1_700_000,
                            "status": "ok",
                        }
                    ],
                },
            )

        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            usage = await client.usage("sk-client-usage-test")
            assert usage.remaining_tokens == 7_000_000
            assert usage.total_requests == 2
            assert usage.models[0].model == "gpt-5"
            assert usage.logs[0].output_tokens == 500_000
            assert client.usage_page_url == "https://token.example.com/token-usage"
        finally:
            await client.close()

    asyncio.run(scenario())
