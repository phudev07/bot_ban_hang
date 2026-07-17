import asyncio
import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Deposit, PaymentTransaction, User
from app.payment_expiry import cleanup_deposit_messages, expire_pending_deposits
from app.services import process_sepay_payment


def payload(transaction_id: int, code: str, amount: int) -> dict[str, object]:
    return {
        "id": transaction_id,
        "transferType": "in",
        "transferAmount": amount,
        "content": code,
    }


class FakeBot:
    def __init__(self) -> None:
        self.deleted: list[tuple[int, int]] = []

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))


async def main() -> None:
    configured_url = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://shop:change_me@postgres:5432/shop",
    )
    database_url = os.environ.get("TEST_DATABASE_URL") or (
        configured_url.rsplit("/", 1)[0] + "/qr_payment_smoke"
    )
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    now = datetime.now(UTC)
    async with sessions() as session:
        session.add(User(telegram_id=99001, full_name="Postgres smoke", balance=0))
        await session.flush()
        session.add_all(
            [
                Deposit(
                    user_id=99001,
                    code="NAP99001A1B2",
                    requested_amount=10_000,
                    expires_at=now + timedelta(minutes=5),
                ),
                Deposit(
                    user_id=99001,
                    code="NAP99001C3D4",
                    requested_amount=20_000,
                    expires_at=now - timedelta(seconds=1),
                    telegram_chat_id=99001,
                    telegram_message_ids="101,102",
                ),
                Deposit(
                    user_id=99001,
                    code="NAP99001E5F6",
                    requested_amount=30_000,
                    expires_at=now + timedelta(minutes=5),
                ),
            ]
        )
        await session.commit()

    concurrent_results = await asyncio.gather(
        process_sepay_payment(sessions, payload(900001, "NAP99001A1B2", 10_000)),
        process_sepay_payment(sessions, payload(900002, "NAP99001A1B2", 10_000)),
    )
    assert {result.status for result in concurrent_results} == {
        "credited",
        "already_paid_payment",
    }

    expiry_results = await asyncio.gather(
        expire_pending_deposits(sessions, now=now),
        process_sepay_payment(sessions, payload(900003, "NAP99001C3D4", 20_000)),
    )
    assert expiry_results[1].status in {"expired_payment", "failed_request_payment"}

    mismatch = await process_sepay_payment(
        sessions,
        payload(900004, "NAP99001E5F6", 29_000),
    )
    assert mismatch.status == "amount_mismatch"

    bot = FakeBot()
    assert await cleanup_deposit_messages(sessions, bot) >= 1
    assert (99001, 101) in bot.deleted and (99001, 102) in bot.deleted

    async with sessions() as session:
        user = await session.get(User, 99001)
        transactions = list(
            await session.scalars(
                select(PaymentTransaction.credit_status).order_by(PaymentTransaction.id)
            )
        )
        assert user is not None and user.balance == 10_000
        assert transactions.count("credited") == 1
        assert "already_paid" in transactions
        assert "amount_mismatch" in transactions
        assert any(status in transactions for status in ("expired", "failed_request"))

    await engine.dispose()
    print("PostgreSQL payment smoke test passed")


if __name__ == "__main__":
    asyncio.run(main())
