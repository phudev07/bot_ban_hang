import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Category, Deposit, InventoryItem, Order, PaymentTransaction, Product, User
from app.payment_expiry import (
    cleanup_deposit_messages,
    expire_pending_deposits,
    register_deposit_message,
)
from app.services import PendingDepositLimitReached, create_deposit, process_sepay_payment
from app.utils import SecretCipher


async def make_database(path: str = ":memory:"):
    url = "sqlite+aiosqlite:///:memory:" if path == ":memory:" else f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


class FakeBot:
    def __init__(self, *, message_gone: bool = False, transient_error: bool = False) -> None:
        self.message_gone = message_gone
        self.transient_error = transient_error
        self.deleted: list[tuple[int, int]] = []

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))
        if self.message_gone:
            raise TelegramBadRequest(
                DeleteMessage(chat_id=chat_id, message_id=message_id),
                "message to delete not found",
            )
        if self.transient_error:
            raise RuntimeError("temporary Telegram outage")


def payment_payload(transaction_id: int, code: str, amount: int) -> dict[str, object]:
    return {
        "id": transaction_id,
        "transferType": "in",
        "transferAmount": amount,
        "content": code,
    }


def test_create_deposit_sets_five_minute_expiry() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            session.add(User(telegram_id=1001, full_name="Buyer"))
            await session.commit()
            before = datetime.now(UTC)
            deposit = await create_deposit(session, 1001, 10_000, expiry_seconds=300)
            after = datetime.now(UTC)
            assert deposit.expires_at is not None
            expires_at = deposit.expires_at.replace(tzinfo=UTC)
            assert before + timedelta(seconds=300) <= expires_at
            assert expires_at <= after + timedelta(seconds=300)
        await engine.dispose()

    asyncio.run(scenario())


def test_identical_deposit_is_reused_and_pending_count_is_limited() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            session.add(User(telegram_id=1010, full_name="Buyer"))
            await session.commit()
            first = await create_deposit(
                session,
                1010,
                10_000,
                expiry_seconds=300,
                max_pending_deposits=2,
            )
            repeated = await create_deposit(
                session,
                1010,
                10_000,
                expiry_seconds=300,
                max_pending_deposits=2,
            )
            second = await create_deposit(
                session,
                1010,
                20_000,
                expiry_seconds=300,
                max_pending_deposits=2,
            )
            assert repeated.id == first.id
            assert second.id != first.id
            with pytest.raises(PendingDepositLimitReached):
                await create_deposit(
                    session,
                    1010,
                    30_000,
                    expiry_seconds=300,
                    max_pending_deposits=2,
                )

        async with sessions() as session:
            assert int(await session.scalar(select(func.count(Deposit.id))) or 0) == 2
        await engine.dispose()

    asyncio.run(scenario())


def test_expiry_marks_only_due_pending_requests_failed() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        now = datetime.now(UTC)
        async with sessions() as session:
            session.add(User(telegram_id=1002, full_name="Buyer"))
            session.add_all(
                [
                    Deposit(
                        user_id=1002,
                        code="NAP1002EXPD",
                        requested_amount=10_000,
                        expires_at=now - timedelta(seconds=1),
                    ),
                    Deposit(
                        user_id=1002,
                        code="NAP1002WAIT",
                        requested_amount=10_000,
                        expires_at=now + timedelta(seconds=1),
                    ),
                    Deposit(
                        user_id=1002,
                        code="NAP1002PAID",
                        requested_amount=10_000,
                        expires_at=now - timedelta(seconds=1),
                        status="paid",
                    ),
                ]
            )
            await session.commit()

        assert await expire_pending_deposits(sessions, now=now) == 1
        async with sessions() as session:
            deposits = {
                item.code: item for item in await session.scalars(select(Deposit))
            }
            assert deposits["NAP1002EXPD"].status == "failed"
            assert deposits["NAP1002EXPD"].failure_reason == "expired"
            assert deposits["NAP1002EXPD"].failed_at is not None
            assert deposits["NAP1002EXPD"].failed_at.replace(tzinfo=UTC) == now
            assert deposits["NAP1002WAIT"].status == "pending"
            assert deposits["NAP1002PAID"].status == "paid"
        await engine.dispose()

    asyncio.run(scenario())


def test_paid_and_failed_qr_messages_are_deleted_and_persisted() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            session.add(User(telegram_id=1003, full_name="Buyer"))
            deposits = [
                Deposit(
                    user_id=1003,
                    code="NAP1003PAID",
                    requested_amount=10_000,
                    status="paid",
                ),
                Deposit(
                    user_id=1003,
                    code="NAP1003FAIL",
                    requested_amount=10_000,
                    status="failed",
                ),
            ]
            session.add_all(deposits)
            await session.commit()
            await register_deposit_message(session, deposits[0].id, 1003, 11)
            await register_deposit_message(session, deposits[1].id, 1003, 12)

        bot = FakeBot()
        assert await cleanup_deposit_messages(sessions, bot) == 2
        assert bot.deleted == [(1003, 11), (1003, 12)]

        async with sessions() as session:
            assert all(
                item.messages_deleted_at is not None
                for item in await session.scalars(select(Deposit))
            )
        assert await cleanup_deposit_messages(sessions, bot) == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_missing_telegram_message_is_treated_as_already_deleted() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            session.add(User(telegram_id=1004, full_name="Buyer"))
            deposit = Deposit(
                user_id=1004,
                code="NAP1004GONE",
                requested_amount=10_000,
                status="failed",
            )
            session.add(deposit)
            await session.commit()
            await register_deposit_message(session, deposit.id, 1004, 13)

        assert await cleanup_deposit_messages(sessions, FakeBot(message_gone=True)) == 1
        async with sessions() as session:
            deposit = await session.get(Deposit, deposit.id)
            assert deposit is not None and deposit.messages_deleted_at is not None
        await engine.dispose()

    asyncio.run(scenario())


def test_transient_telegram_failure_is_retried() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            session.add(User(telegram_id=1007, full_name="Buyer"))
            deposit = Deposit(
                user_id=1007,
                code="NAP1007RETR",
                requested_amount=10_000,
                status="failed",
            )
            session.add(deposit)
            await session.commit()
            await register_deposit_message(session, deposit.id, 1007, 14)

        assert await cleanup_deposit_messages(
            sessions, FakeBot(transient_error=True)
        ) == 0
        async with sessions() as session:
            deposit = await session.get(Deposit, deposit.id)
            assert deposit is not None and deposit.messages_deleted_at is None
        assert await cleanup_deposit_messages(sessions, FakeBot()) == 1
        await engine.dispose()

    asyncio.run(scenario())


def test_expiry_survives_process_restart(tmp_path) -> None:
    async def scenario() -> None:
        database_path = (tmp_path / "expiry-restart.db").as_posix()
        engine, sessions = await make_database(database_path)
        now = datetime.now(UTC)
        async with sessions() as session:
            session.add(User(telegram_id=1008, full_name="Buyer"))
            session.add(
                Deposit(
                    user_id=1008,
                    code="NAP1008REST",
                    requested_amount=10_000,
                    expires_at=now - timedelta(seconds=1),
                )
            )
            await session.commit()
        await engine.dispose()

        restarted_engine, restarted_sessions = await make_database(database_path)
        assert await expire_pending_deposits(restarted_sessions, now=now) == 1
        async with restarted_sessions() as session:
            deposit = await session.scalar(select(Deposit).where(Deposit.code == "NAP1008REST"))
            assert deposit is not None and deposit.status == "failed"
            assert deposit.failure_reason == "expired"
        await restarted_engine.dispose()

    asyncio.run(scenario())


def test_payment_before_expiry_is_credited_once() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            session.add(User(telegram_id=1009, full_name="Buyer", balance=0))
            session.add(
                Deposit(
                    user_id=1009,
                    code="NAP10090B1E1",
                    requested_amount=10_000,
                    expires_at=datetime.now(UTC) + timedelta(seconds=2),
                )
            )
            await session.commit()

        result = await process_sepay_payment(
            sessions, payment_payload(701, "NAP10090B1E1", 10_000)
        )
        duplicate = await process_sepay_payment(
            sessions, payment_payload(701, "NAP10090B1E1", 10_000)
        )
        assert result.status == "credited"
        assert duplicate.status == "duplicate"
        async with sessions() as session:
            user = await session.get(User, 1009)
            assert user is not None and user.balance == 10_000
        await engine.dispose()

    asyncio.run(scenario())


def test_late_mismatched_and_reused_payments_never_credit_wallet() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        now = datetime.now(UTC)
        async with sessions() as session:
            user = User(telegram_id=1005, full_name="Buyer", balance=0)
            session.add(user)
            session.add_all(
                [
                    Deposit(
                        user_id=1005,
                        code="NAP10050A1E1",
                        requested_amount=10_000,
                        expires_at=now - timedelta(seconds=1),
                    ),
                    Deposit(
                        user_id=1005,
                        code="NAP10050A2E2",
                        requested_amount=20_000,
                        expires_at=now + timedelta(minutes=5),
                    ),
                    Deposit(
                        user_id=1005,
                        code="NAP10050A3E3",
                        requested_amount=30_000,
                        expires_at=now + timedelta(minutes=5),
                    ),
                ]
            )
            await session.commit()

        late = await process_sepay_payment(
            sessions, payment_payload(501, "NAP10050A1E1", 10_000)
        )
        mismatch = await process_sepay_payment(
            sessions, payment_payload(502, "NAP10050A2E2", 19_000)
        )
        paid = await process_sepay_payment(
            sessions, payment_payload(503, "NAP10050A3E3", 30_000)
        )
        reused = await process_sepay_payment(
            sessions, payment_payload(504, "NAP10050A3E3", 30_000)
        )

        assert late.status == "expired_payment"
        assert mismatch.status == "amount_mismatch"
        assert paid.status == "credited"
        assert reused.status == "already_paid_payment"

        async with sessions() as session:
            user = await session.get(User, 1005)
            statuses = list(
                await session.scalars(
                    select(PaymentTransaction.credit_status).order_by(PaymentTransaction.id)
                )
            )
            assert user is not None and user.balance == 30_000
            assert statuses == ["expired", "amount_mismatch", "credited", "already_paid"]
        await engine.dispose()

    asyncio.run(scenario())


def test_expired_direct_purchase_does_not_consume_stock() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tài khoản",
                name_en="Account",
                price=50_000,
            )
            user = User(telegram_id=1006, full_name="Buyer", balance=0)
            session.add_all([product, user])
            await session.flush()
            session.add_all(
                [
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt("account:password"),
                    ),
                    Deposit(
                        user_id=1006,
                        code="NAP10060A4E4",
                        requested_amount=50_000,
                        payment_kind="direct_purchase",
                        product_id=product.id,
                        expires_at=datetime.now(UTC) - timedelta(seconds=1),
                    ),
                ]
            )
            await session.commit()

        result = await process_sepay_payment(
            sessions,
            payment_payload(601, "NAP10060A4E4", 50_000),
            cipher=cipher,
        )
        assert result.status == "expired_payment"
        async with sessions() as session:
            stock = await session.scalar(select(InventoryItem))
            order_count = await session.scalar(select(func.count(Order.id)))
            user = await session.get(User, 1006)
            assert stock is not None and stock.status == "available"
            assert order_count == 0
            assert user is not None and user.balance == 0
        await engine.dispose()

    asyncio.run(scenario())
