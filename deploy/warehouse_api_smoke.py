import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time

import httpx
from sqlalchemy import func, select

from app.config import Settings
from app.database import create_database
from app.models import ApiClient, Product
from app.suppliers import SELLABLE_FULFILLMENT_SOURCES
from app.utils import SecretCipher


def signed_headers(
    api_id: str,
    api_secret: str,
    method: str,
    path: str,
    body: bytes = b"",
    *,
    idempotency_key: str | None = None,
) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    canonical = "|".join(
        (
            timestamp,
            nonce,
            method.upper(),
            path,
            hashlib.sha256(body).hexdigest(),
        )
    )
    signature = hmac.new(
        api_secret.encode(),
        canonical.encode(),
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "X-Shop-API-ID": api_id,
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": signature,
    }
    if body:
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


async def main() -> None:
    settings = Settings()
    engine, sessions = create_database(
        settings.database_url,
        pool_size=2,
        max_overflow=0,
        pool_timeout=5,
    )
    cipher = SecretCipher(settings.inventory_encryption_key.get_secret_value())
    try:
        async with sessions() as session:
            api_client = await session.scalar(
                select(ApiClient)
                .where(
                    ApiClient.active.is_(True),
                    ApiClient.admin_blocked.is_(False),
                    ApiClient.allowed_ips == "",
                )
                .order_by(ApiClient.last_used_at.desc().nullslast(), ApiClient.id.desc())
                .limit(1)
            )
            expected_products = int(
                await session.scalar(
                    select(func.count(Product.id)).where(
                        Product.active.is_(True),
                        Product.product_type == "account",
                        Product.fulfillment_source.in_(SELLABLE_FULFILLMENT_SOURCES),
                    )
                )
                or 0
            )
        if api_client is None:
            raise RuntimeError("No active unrestricted API client is available for smoke testing")

        api_secret = cipher.decrypt(api_client.encrypted_secret)
        base_url = os.environ.get("SHOP_API_SMOKE_BASE_URL", "http://app:8080")
        async with httpx.AsyncClient(base_url=base_url, timeout=20, trust_env=False) as client:
            account_path = "/v1/account"
            account = await client.get(
                account_path,
                headers=signed_headers(
                    api_client.api_id,
                    api_secret,
                    "GET",
                    account_path,
                ),
            )
            assert account.status_code == 200, account.text
            assert account.headers.get("cache-control") == "no-store"

            products_path = "/v1/products"
            products = await client.get(
                products_path,
                headers=signed_headers(
                    api_client.api_id,
                    api_secret,
                    "GET",
                    products_path,
                ),
            )
            assert products.status_code == 200, products.text
            catalog = products.json()
            assert catalog["count"] == expected_products
            assert catalog["products"]
            assert products.headers.get("cache-control") == "no-store"

            body = json.dumps(
                {"product_id": catalog["products"][0]["id"], "quantity": 1},
                separators=(",", ":"),
            ).encode()
            rejected = await client.post(
                "/v1/orders",
                content=body,
                headers=signed_headers(
                    api_client.api_id,
                    api_secret,
                    "POST",
                    "/v1/orders",
                    body,
                    idempotency_key=f"smoke-{secrets.token_hex(10)}",
                ),
            )
            assert rejected.status_code == 400, rejected.text
            assert rejected.json()["detail"]["code"] == "MAX_UNIT_PRICE_REQUIRED"

            replay_headers = signed_headers(
                api_client.api_id,
                api_secret,
                "GET",
                account_path,
            )
            assert (await client.get(account_path, headers=replay_headers)).status_code == 200
            replay = await client.get(account_path, headers=replay_headers)
            assert replay.status_code == 409
            assert replay.json()["detail"]["code"] == "REPLAYED_REQUEST"

        print(f"Warehouse API production smoke passed: products={expected_products}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
