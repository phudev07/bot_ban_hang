import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.haji_pending import HajiPendingFailure, settle_haji_attempt
from app.haji_suppliers import HajiOrderStatus
from app.models import (
    Category,
    Deposit,
    InventoryItem,
    Order,
    PaymentTransaction,
    Product,
    SupplierPurchaseAttempt,
    User,
    WalletTransaction,
)
from app.utils import SecretCipher


class FailedHajiOrder:
    async def check_order(self, order_code: str) -> HajiOrderStatus:
        assert order_code == "HAJI-AP-FAILED001"
        return HajiOrderStatus(
            order_code=order_code,
            product_id="claude_addteam1x25",
            quantity=1,
            status="failed",
            unit_price=400_000,
            items=(),
        )


class CompletedHajiOrderWithoutCost:
    async def check_order(self, order_code: str) -> HajiOrderStatus:
        assert order_code == "HAJI-AP-DONE001"
        return HajiOrderStatus(
            order_code=order_code,
            product_id="claude_addteam1x25",
            quantity=1,
            status="done",
            unit_price=0,
            items=("customer@example.com",),
        )

    async def fetch_snapshot(self, product_id: str) -> SimpleNamespace:
        assert product_id == "claude_addteam1x25"
        return SimpleNamespace(unit_price=400_000)


def test_failed_haji_claude_order_refunds_paid_amount_once() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())

        async with sessions() as session:
            category = Category(name_vi="Tài Khoản ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Claude Team",
                name_en="Claude Team",
                price=450_000,
                fulfillment_source="haji",
                supplier_product_id="claude_addteam1x25",
            )
            user = User(telegram_id=9001, full_name="Buyer")
            session.add_all([product, user])
            await session.flush()
            deposit = Deposit(
                user_id=user.telegram_id,
                code="NAP9001FAILED",
                requested_amount=450_000,
                payment_kind="direct_purchase",
                product_id=product.id,
                quantity=1,
                status="paid",
                paid_amount=450_000,
            )
            session.add(deposit)
            await session.flush()
            session.add(
                PaymentTransaction(
                    deposit_id=deposit.id,
                    user_id=user.telegram_id,
                    provider_tx_id="SEPAY-FAILED-001",
                    amount=450_000,
                    credit_status="credited",
                )
            )
            session.add(
                SupplierPurchaseAttempt(
                    provider="haji",
                    request_key="qr-NAP9001FAILED",
                    product_id=product.id,
                    supplier_product_id="claude_addteam1x25",
                    quantity=1,
                    status="processing",
                    supplier_order_code="HAJI-AP-FAILED001",
                    deposit_id=deposit.id,
                    started_at=datetime.now(UTC),
                )
            )
            await session.commit()

        async with sessions() as session:
            attempt = await session.scalar(select(SupplierPurchaseAttempt))
            assert attempt is not None
            attempt_id = attempt.id

        result = await settle_haji_attempt(
            sessions,
            FailedHajiOrder(),  # type: ignore[arg-type]
            cipher,
            attempt_id,
        )
        assert isinstance(result, HajiPendingFailure)
        assert result.amount == 450_000 and result.balance == 450_000

        async with sessions() as session:
            user = await session.get(User, 9001)
            attempt = await session.get(SupplierPurchaseAttempt, attempt_id)
            payment = await session.scalar(select(PaymentTransaction))
            refund = await session.scalar(select(WalletTransaction))
            assert user is not None and user.balance == 450_000
            assert attempt is not None and attempt.status == "failed"
            assert payment is not None and payment.credit_status == "refunded"
            assert refund is not None
            assert refund.kind == "direct_purchase_refund"
            assert refund.event_key == "direct-purchase-refund:1"
        await engine.dispose()

    asyncio.run(scenario())


def test_completed_haji_order_resolves_missing_cost_from_catalog() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())

        async with sessions() as session:
            category = Category(name_vi="Tài Khoản ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Claude Team",
                name_en="Claude Team",
                price=450_000,
                fulfillment_source="haji",
                supplier_product_id="claude_addteam1x25",
            )
            user = User(telegram_id=9002, full_name="Buyer")
            session.add_all([product, user])
            await session.flush()
            deposit = Deposit(
                user_id=user.telegram_id,
                code="NAP9002DONE",
                requested_amount=450_000,
                payment_kind="direct_purchase",
                product_id=product.id,
                quantity=1,
                status="paid",
                paid_amount=450_000,
            )
            session.add(deposit)
            await session.flush()
            attempt = SupplierPurchaseAttempt(
                provider="haji",
                request_key="qr-NAP9002DONE",
                product_id=product.id,
                supplier_product_id="claude_addteam1x25",
                quantity=1,
                status="processing",
                error_code="SUPPLIER_PENDING",
                supplier_order_code="HAJI-AP-DONE001",
                deposit_id=deposit.id,
                started_at=datetime.now(UTC),
            )
            session.add(attempt)
            await session.commit()
            attempt_id = attempt.id

        result = await settle_haji_attempt(
            sessions,
            CompletedHajiOrderWithoutCost(),  # type: ignore[arg-type]
            cipher,
            attempt_id,
        )
        assert result is not None
        assert result.amount == 450_000
        assert result.secrets == ("customer@example.com",)

        async with sessions() as session:
            attempt = await session.get(SupplierPurchaseAttempt, attempt_id)
            order = await session.scalar(select(Order))
            item = await session.scalar(select(InventoryItem))
            assert attempt is not None and attempt.status == "succeeded"
            assert order is not None and order.cost_amount == 400_000
            assert item is not None and item.cost_amount == 400_000
        await engine.dispose()

    asyncio.run(scenario())
