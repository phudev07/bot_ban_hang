import asyncio
import json
import os
import secrets
import time
from datetime import UTC, datetime, timedelta

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import create_api
from app.config import Settings
from app.dashboard_security import hash_dashboard_password
from app.database import Base
from app.models import (
    ApiClient,
    ApiOrderRequest,
    Category,
    DiscountCode,
    InventoryItem,
    Order,
    PaymentTransaction,
    Product,
    ReferralReward,
    SupplierBalanceTransaction,
    User,
)
from app.partner_services import api_signature, ensure_api_client, rotate_api_secret
from app.services import PendingDepositLimitReached, create_deposit, purchase_product
from app.supplier_audit import reconcile_supplier_balance
from app.suppliers import SupplierPurchase, SupplierSnapshot
from app.utils import SecretCipher


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **_kwargs) -> None:
        self.messages.append((chat_id, text))


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


class FakeSupplier:
    def __init__(self, balance: int = 100_000) -> None:
        self.balance = balance
        self.balance_lock = asyncio.Lock()
        self.buy_count = 0

    async def fetch_balance(self) -> int:
        return self.balance

    async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
        return SupplierSnapshot(
            product_id=product_id,
            name="Dynamic supplier account",
            description="",
            unit_price=15_000,
            source_stock=100,
            owner_balance=self.balance,
        )

    async def buy(self, _product_id: str, quantity: int) -> SupplierPurchase:
        cost = 15_000 * quantity
        assert self.balance >= cost
        self.balance -= cost
        self.buy_count += 1
        return SupplierPurchase(
            order_code=f"SUMI-E2E-{self.buy_count}",
            unit_price=15_000,
            accounts=tuple(
                f"supplier-{self.buy_count}-{index}|password"
                for index in range(1, quantity + 1)
            ),
        )


def payment_payload(transaction_id: int, code: str, amount: int) -> dict[str, object]:
    return {
        "id": transaction_id,
        "transferType": "in",
        "transferAmount": amount,
        "content": code,
    }


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
    if body:
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


async def create_api_client_concurrently(sessions, user_id: int, cipher: SecretCipher):
    async def create_one():
        async with sessions() as session:
            result = await ensure_api_client(session, user_id, cipher, 60)
            await session.commit()
            return result

    first, second = await asyncio.gather(create_one(), create_one())
    assert first[0].id == second[0].id
    secrets_created = [secret for _, secret in (first, second) if secret]
    assert len(secrets_created) == 1
    return first[0].api_id, secrets_created[0]


async def main() -> None:
    configured_url = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://shop:change_me@postgres:5432/shop",
    )
    database_name = os.environ.get("TEST_DATABASE_NAME", "shop_e2e")
    database_url = os.environ.get("TEST_DATABASE_URL") or (
        configured_url.rsplit("/", 1)[0] + f"/{database_name}"
    )
    engine = create_async_engine(database_url, pool_size=10, max_overflow=10)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    encryption_key = Fernet.generate_key().decode()
    cipher = SecretCipher(encryption_key)
    supplier = FakeSupplier()
    bot = FakeBot()
    redis = FakeRedis()
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=encryption_key,
        sepay_enabled=True,
        sepay_auth_mode="api_key",
        sepay_api_key="e2e-sepay-key",
        bank_code="TEST",
        bank_account="0000000000",
        bank_account_name="E2E SHOP",
        dashboard_enabled=True,
        dashboard_username="admin",
        dashboard_password_hash=hash_dashboard_password("e2e-admin-password"),
        dashboard_session_secret="e2e-session-secret-long-enough",
        shop_api_enabled=True,
        shop_api_base_url="https://token.vietshare.site/v1",
        referral_commission_percent=5,
        seed_demo_data=False,
    )

    async with sessions() as session:
        referrer = User(
            telegram_id=991001,
            full_name="E2E Referrer",
            balance=0,
            referral_code="REFE2E001",
            has_started=True,
        )
        buyer = User(
            telegram_id=991002,
            full_name="E2E Wallet Buyer",
            balance=0,
            referred_by_id=referrer.telegram_id,
            has_started=True,
        )
        direct_buyer = User(
            telegram_id=991003,
            full_name="E2E Direct Buyer",
            balance=0,
            referred_by_id=referrer.telegram_id,
            has_started=True,
        )
        race_buyer_a = User(
            telegram_id=991004,
            full_name="E2E Race A",
            balance=50_000,
            has_started=True,
        )
        race_buyer_b = User(
            telegram_id=991005,
            full_name="E2E Race B",
            balance=50_000,
            has_started=True,
        )
        api_buyer = User(
            telegram_id=991006,
            full_name="E2E API Buyer",
            balance=80_000,
            referred_by_id=referrer.telegram_id,
            has_started=True,
        )
        supplier_buyer = User(
            telegram_id=991007,
            full_name="E2E Supplier Buyer",
            balance=50_000,
            has_started=True,
        )
        category = Category(name_vi="E2E Accounts", name_en="E2E Accounts")
        session.add_all(
            [
                referrer,
                buyer,
                direct_buyer,
                race_buyer_a,
                race_buyer_b,
                api_buyer,
                supplier_buyer,
                category,
            ]
        )
        await session.flush()

        bulk_product = Product(
            category_id=category.id,
            name_vi="E2E Bulk Account",
            name_en="E2E Bulk Account",
            price=20_000,
            allow_quantity=True,
            max_quantity=10,
        )
        direct_product = Product(
            category_id=category.id,
            name_vi="E2E Direct Account",
            name_en="E2E Direct Account",
            price=12_000,
        )
        race_product = Product(
            category_id=category.id,
            name_vi="E2E Last Stock",
            name_en="E2E Last Stock",
            price=10_000,
        )
        empty_product = Product(
            category_id=category.id,
            name_vi="E2E Empty",
            name_en="E2E Empty",
            price=10_000,
        )
        expensive_product = Product(
            category_id=category.id,
            name_vi="E2E Expensive",
            name_en="E2E Expensive",
            price=1_000_000,
        )
        supplier_product = Product(
            category_id=category.id,
            name_vi="E2E Dynamic Supplier",
            name_en="E2E Dynamic Supplier",
            price=99_000,
            allow_quantity=True,
            max_quantity=10,
            fulfillment_source="sumistore",
            supplier_product_id="SP-E2E",
            supplier_markup=5_000,
        )
        session.add_all(
            [
                bulk_product,
                direct_product,
                race_product,
                empty_product,
                expensive_product,
                supplier_product,
            ]
        )
        await session.flush()
        bulk_coupon = DiscountCode(
            product_id=bulk_product.id,
            code="BULK5K",
            discount_type="fixed",
            discount_value=5_000,
            max_uses=10,
        )
        direct_coupon = DiscountCode(
            product_id=direct_product.id,
            code="DIRECT2K",
            discount_type="fixed",
            discount_value=2_000,
            max_uses=10,
        )
        session.add_all([bulk_coupon, direct_coupon])
        session.add_all(
            [
                InventoryItem(
                    product_id=bulk_product.id,
                    encrypted_secret=cipher.encrypt(f"bulk-{index}|password"),
                )
                for index in range(1, 5)
            ]
            + [
                InventoryItem(
                    product_id=direct_product.id,
                    encrypted_secret=cipher.encrypt("direct-1|password"),
                ),
                InventoryItem(
                    product_id=race_product.id,
                    encrypted_secret=cipher.encrypt("race-1|password"),
                ),
                InventoryItem(
                    product_id=expensive_product.id,
                    encrypted_secret=cipher.encrypt("expensive-1|password"),
                ),
            ]
        )
        await session.commit()
        ids = {
            "bulk": bulk_product.id,
            "direct": direct_product.id,
            "race": race_product.id,
            "empty": empty_product.id,
            "expensive": expensive_product.id,
            "supplier": supplier_product.id,
            "bulk_coupon": bulk_coupon.id,
            "direct_coupon": direct_coupon.id,
        }

    api_id, api_secret = await create_api_client_concurrently(
        sessions, 991006, cipher
    )
    app = create_api(
        settings,
        sessions,
        bot,  # type: ignore[arg-type]
        cipher,
        supplier,  # type: ignore[arg-type]
        api_redis=redis,  # type: ignore[arg-type]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        follow_redirects=False,
    ) as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.headers["x-frame-options"] == "DENY"
        assert (await client.get("/docs")).status_code == 200
        assert (await client.get("/v1")).status_code == 200
        assert (await client.get("/v1/health")).status_code == 200

        protected = await client.get("/admin")
        assert protected.status_code == 303
        assert protected.headers["location"] == "/admin/login"
        assert (
            await client.post(
                "/admin/login",
                data={"username": "admin", "password": "wrong"},
            )
        ).status_code == 401
        login = await client.post(
            "/admin/login",
            data={"username": "admin", "password": "e2e-admin-password"},
        )
        assert login.status_code == 303

        async with sessions() as session:
            wallet_deposit = await create_deposit(session, 991002, 30_000)
        webhook_headers = {"Authorization": "Apikey e2e-sepay-key"}
        unauthorized = await client.post(
            "/webhooks/sepay",
            json=payment_payload(810000, wallet_deposit.code, 30_000),
        )
        assert unauthorized.status_code == 401
        first_payment, repeated_payment = await asyncio.gather(
            client.post(
                "/webhooks/sepay",
                headers=webhook_headers,
                json=payment_payload(810001, wallet_deposit.code, 30_000),
            ),
            client.post(
                "/webhooks/sepay",
                headers=webhook_headers,
                json=payment_payload(810002, wallet_deposit.code, 30_000),
            ),
        )
        assert {first_payment.json()["status"], repeated_payment.json()["status"]} == {
            "credited",
            "already_paid_payment",
        }

        async with sessions() as session:
            mismatch_deposit = await create_deposit(session, 991002, 11_000)
            expired_deposit = await create_deposit(session, 991002, 12_000)
            expired_deposit.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        mismatch = await client.post(
            "/webhooks/sepay",
            headers=webhook_headers,
            json=payment_payload(810003, mismatch_deposit.code, 10_000),
        )
        expired = await client.post(
            "/webhooks/sepay",
            headers=webhook_headers,
            json=payment_payload(810004, expired_deposit.code, 12_000),
        )
        assert mismatch.json()["status"] == "amount_mismatch"
        assert expired.json()["status"] == "expired_payment"

        async def create_concurrent_qr(amount: int) -> str:
            async with sessions() as session:
                try:
                    await create_deposit(
                        session,
                        991004,
                        amount,
                        max_pending_deposits=3,
                    )
                except PendingDepositLimitReached:
                    return "limited"
                return "created"

        qr_results = await asyncio.gather(
            *(create_concurrent_qr(amount) for amount in (41_000, 42_000, 43_000, 44_000))
        )
        assert sorted(qr_results) == ["created", "created", "created", "limited"]

        multi = await purchase_product(
            sessions,
            991002,
            ids["bulk"],
            cipher,
            quantity=2,
            coupon_code="BULK5K",
            referral_commission_percent=5,
        )
        assert multi.ok and len(multi.orders) == 2
        assert multi.total_amount == 30_000
        assert len({order.shop_order_code for order in multi.orders}) == 1

        async with sessions() as session:
            direct_deposit = await create_deposit(
                session,
                991003,
                10_000,
                payment_kind="direct_purchase",
                product_id=ids["direct"],
                quantity=1,
                discount_amount=2_000,
                discount_code_id=ids["direct_coupon"],
                discount_code="DIRECT2K",
            )
        direct = await client.post(
            "/webhooks/sepay",
            headers=webhook_headers,
            json=payment_payload(810005, direct_deposit.code, 10_000),
        )
        assert direct.json()["status"] == "direct_purchase_completed"

        race_results = await asyncio.gather(
            purchase_product(sessions, 991004, ids["race"], cipher),
            purchase_product(sessions, 991005, ids["race"], cipher),
        )
        assert sum(result.ok for result in race_results) == 1
        assert sorted(result.message for result in race_results) == ["completed", "out_of_stock"]

        await reconcile_supplier_balance(sessions, supplier)  # type: ignore[arg-type]
        supplier_sale = await purchase_product(
            sessions,
            991007,
            ids["supplier"],
            cipher,
            quantity=2,
            supplier_client=supplier,  # type: ignore[arg-type]
        )
        assert supplier_sale.ok and supplier_sale.total_amount == 40_000
        reconciled = await reconcile_supplier_balance(sessions, supplier)  # type: ignore[arg-type]
        assert reconciled.expected_purchase_debit == 30_000
        assert reconciled.suspicious_amount == 0
        supplier.balance -= 7_000
        suspicious = await reconcile_supplier_balance(sessions, supplier)  # type: ignore[arg-type]
        repeated_audit = await reconcile_supplier_balance(sessions, supplier)  # type: ignore[arg-type]
        assert suspicious.suspicious_amount == -7_000
        assert repeated_audit.suspicious_amount == 0

        order_body = json.dumps(
            {
                "product_id": ids["bulk"],
                "quantity": 1,
                "coupon_code": None,
                "max_unit_price": 20_000,
            },
            separators=(",", ":"),
        ).encode()
        idempotency_key = "E2E-API-ORDER-0001"
        api_calls = await asyncio.gather(
            client.post(
                "/v1/orders",
                content=order_body,
                headers=signed_headers(
                    api_id,
                    api_secret,
                    "POST",
                    "/v1/orders",
                    order_body,
                    idempotency_key=idempotency_key,
                ),
            ),
            client.post(
                "/v1/orders",
                content=order_body,
                headers=signed_headers(
                    api_id,
                    api_secret,
                    "POST",
                    "/v1/orders",
                    order_body,
                    idempotency_key=idempotency_key,
                ),
            ),
        )
        assert sorted(response.status_code for response in api_calls) in ([200, 200], [200, 409])
        completed_api = next(response for response in api_calls if response.status_code == 200)
        api_order_code = completed_api.json()["order"]["order_code"]
        repeated_api = await client.post(
            "/v1/orders",
            content=order_body,
            headers=signed_headers(
                api_id,
                api_secret,
                "POST",
                "/v1/orders",
                order_body,
                idempotency_key=idempotency_key,
            ),
        )
        assert repeated_api.status_code == 200
        assert repeated_api.json()["order"]["order_code"] == api_order_code

        mismatch_body = json.dumps(
            {
                "product_id": ids["bulk"],
                "quantity": 2,
                "coupon_code": None,
                "max_unit_price": 20_000,
            },
            separators=(",", ":"),
        ).encode()
        api_mismatch = await client.post(
            "/v1/orders",
            content=mismatch_body,
            headers=signed_headers(
                api_id,
                api_secret,
                "POST",
                "/v1/orders",
                mismatch_body,
                idempotency_key=idempotency_key,
            ),
        )
        assert api_mismatch.status_code == 409
        assert api_mismatch.json()["detail"]["code"] == "IDEMPOTENCY_MISMATCH"

        expensive_body = json.dumps(
            {
                "product_id": ids["expensive"],
                "quantity": 1,
                "coupon_code": None,
                "max_unit_price": 999_999,
            },
            separators=(",", ":"),
        ).encode()
        insufficient = await client.post(
            "/v1/orders",
            content=expensive_body,
            headers=signed_headers(
                api_id,
                api_secret,
                "POST",
                "/v1/orders",
                expensive_body,
                idempotency_key="E2E-INSUFFICIENT-001",
            ),
        )
        assert insufficient.status_code == 402

        empty_body = json.dumps(
            {
                "product_id": ids["empty"],
                "quantity": 1,
                "coupon_code": None,
                "max_unit_price": 10_000,
            },
            separators=(",", ":"),
        ).encode()
        out_of_stock = await client.post(
            "/v1/orders",
            content=empty_body,
            headers=signed_headers(
                api_id,
                api_secret,
                "POST",
                "/v1/orders",
                empty_body,
                idempotency_key="E2E-OUT-OF-STOCK-001",
            ),
        )
        assert out_of_stock.status_code == 409

        account_headers = signed_headers(api_id, api_secret, "GET", "/v1/account")
        assert (await client.get("/v1/account", headers=account_headers)).status_code == 200
        replay = await client.get("/v1/account", headers=account_headers)
        assert replay.status_code == 409
        assert replay.json()["detail"]["code"] == "REPLAYED_REQUEST"
        assert (
            await client.get(
                "/v1/orders",
                headers=signed_headers(api_id, api_secret, "GET", "/v1/orders"),
            )
        ).status_code == 200
        assert (
            await client.get(
                f"/v1/orders/{api_order_code}",
                headers=signed_headers(
                    api_id,
                    api_secret,
                    "GET",
                    f"/v1/orders/{api_order_code}",
                ),
            )
        ).status_code == 200

        async with sessions() as session:
            _, new_secret = await rotate_api_secret(session, 991006, cipher)
            await session.commit()
        old_secret_response = await client.get(
            "/v1/account",
            headers=signed_headers(api_id, api_secret, "GET", "/v1/account"),
        )
        new_secret_response = await client.get(
            "/v1/account",
            headers=signed_headers(api_id, new_secret, "GET", "/v1/account"),
        )
        assert old_secret_response.status_code == 401
        assert new_secret_response.status_code == 200

        admin_pages = (
            "/admin",
            "/admin/orders",
            "/admin/payments",
            "/admin/products",
            "/admin/categories",
            "/admin/inventory",
            "/admin/discounts",
            "/admin/users",
            "/admin/api-clients",
            "/admin/referrals",
            "/admin/supplier-audit",
            "/admin/system",
        )
        for page in admin_pages:
            response = await client.get(page)
            assert response.status_code == 200, page
        orders_page = await client.get("/admin/orders")
        assert multi.orders[0].shop_order_code in orders_page.text
        assert api_order_code in orders_page.text
        supplier_page = await client.get("/admin/supplier-audit")
        assert "7.000" in supplier_page.text

    async with sessions() as session:
        buyer = await session.get(User, 991002)
        direct_buyer = await session.get(User, 991003)
        referrer = await session.get(User, 991001)
        api_buyer = await session.get(User, 991006)
        supplier_product = await session.get(Product, ids["supplier"])
        bulk_coupon = await session.get(DiscountCode, ids["bulk_coupon"])
        direct_coupon = await session.get(DiscountCode, ids["direct_coupon"])
        api_client_count = int(await session.scalar(select(func.count(ApiClient.id))) or 0)
        api_request_count = int(
            await session.scalar(
                select(func.count(ApiOrderRequest.id)).where(
                    ApiOrderRequest.idempotency_key == "E2E-API-ORDER-0001"
                )
            )
            or 0
        )
        api_order_count = int(
            await session.scalar(
                select(func.count(Order.id)).where(Order.batch_code == api_order_code)
            )
            or 0
        )
        race_order_count = int(
            await session.scalar(
                select(func.count(Order.id)).where(Order.product_id == ids["race"])
            )
            or 0
        )
        rewards = list(await session.scalars(select(ReferralReward).order_by(ReferralReward.id)))
        payment_statuses = list(
            await session.scalars(
                select(PaymentTransaction.credit_status).order_by(PaymentTransaction.id)
            )
        )
        suspicious_rows = list(
            await session.scalars(
                select(SupplierBalanceTransaction).where(
                    SupplierBalanceTransaction.kind == "suspicious"
                )
            )
        )

        assert buyer is not None and buyer.balance == 0
        assert direct_buyer is not None and direct_buyer.balance == 0
        assert api_buyer is not None and api_buyer.balance == 60_000
        assert referrer is not None and referrer.balance == 3_000
        assert bulk_coupon is not None and bulk_coupon.used_count == 1
        assert direct_coupon is not None and direct_coupon.used_count == 1
        assert supplier_product is not None and supplier_product.price == 20_000
        assert api_client_count == 1
        assert api_request_count == 1
        assert api_order_count == 1
        assert race_order_count == 1
        assert len(rewards) == 3
        assert sum(reward.commission_amount for reward in rewards) == 3_000
        assert payment_statuses.count("credited") == 2
        assert "already_paid" in payment_statuses
        assert "amount_mismatch" in payment_statuses
        assert "expired" in payment_statuses
        assert len(suspicious_rows) == 1 and suspicious_rows[0].amount == -7_000
        assert any(chat_id == 991002 for chat_id, _ in bot.messages)
        assert any(chat_id == 991003 for chat_id, _ in bot.messages)

    await engine.dispose()
    print("PostgreSQL whole-shop E2E passed")


if __name__ == "__main__":
    asyncio.run(main())
