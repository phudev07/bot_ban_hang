import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.main import notify_unresolved_supplier_alerts
from app.models import (
    Category,
    Product,
    SupplierBalanceTransaction,
    SupplierRecoveryRequest,
)
from app.supplier_audit import pending_unresolved_supplier_alerts


async def make_database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


class FakeBot:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        if self.fail:
            raise RuntimeError("telegram unavailable")
        self.messages.append((chat_id, text))


def test_supplier_alerts_wait_for_provider_recovery_window() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        now = datetime.now(UTC)
        async with sessions() as session:
            session.add_all(
                [
                    SupplierBalanceTransaction(
                        provider="sumistore",
                        kind="suspicious",
                        amount=-10_000,
                        balance_before=100_000,
                        balance_after=90_000,
                        created_at=now - timedelta(hours=23),
                    ),
                    SupplierBalanceTransaction(
                        provider="sumistore",
                        kind="suspicious",
                        amount=-20_000,
                        balance_before=90_000,
                        balance_after=70_000,
                        created_at=now - timedelta(hours=25),
                    ),
                    SupplierBalanceTransaction(
                        provider="lehai",
                        kind="suspicious",
                        amount=-27_000,
                        balance_before=200_000,
                        balance_after=173_000,
                        created_at=now - timedelta(hours=47),
                    ),
                    SupplierBalanceTransaction(
                        provider="lehai",
                        kind="suspicious",
                        amount=-35_000,
                        balance_before=173_000,
                        balance_after=138_000,
                        created_at=now - timedelta(hours=49),
                    ),
                ]
            )
            await session.commit()

        sumi_alerts = await pending_unresolved_supplier_alerts(
            sessions,
            provider="sumistore",
            now=now,
        )
        lehai_alerts = await pending_unresolved_supplier_alerts(
            sessions,
            provider="lehai",
            now=now,
        )

        assert [alert.amount for alert in sumi_alerts] == [-20_000]
        assert [alert.amount for alert in lehai_alerts] == [-35_000]
        await engine.dispose()

    asyncio.run(scenario())


def test_active_sumistore_recovery_suppresses_admin_alert() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        now = datetime.now(UTC)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus",
                name_en="GPT Plus",
                price=15_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-ALERT-RECOVERY",
            )
            audit = SupplierBalanceTransaction(
                provider="sumistore",
                kind="suspicious",
                amount=-10_000,
                balance_before=100_000,
                balance_after=90_000,
                created_at=now - timedelta(hours=25),
            )
            session.add_all([product, audit])
            await session.flush()
            session.add(
                SupplierRecoveryRequest(
                    provider="sumistore",
                    request_key=f"audit-{audit.id}-API-PENDING",
                    product_id=product.id,
                    supplier_product_id="SP-ALERT-RECOVERY",
                    quantity=1,
                    status="pending",
                    error_code="MISSING_LOCAL_COMMIT",
                    started_at=now - timedelta(minutes=1),
                    expires_at=now + timedelta(hours=1),
                )
            )
            await session.commit()

        alerts = await pending_unresolved_supplier_alerts(
            sessions,
            provider="sumistore",
            now=now,
        )
        assert alerts == ()
        await engine.dispose()

    asyncio.run(scenario())


def test_resolved_supplier_anomalies_never_alert() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        now = datetime.now(UTC)
        async with sessions() as session:
            session.add_all(
                [
                    SupplierBalanceTransaction(
                        provider="sumistore",
                        kind="recovered",
                        amount=-10_000,
                        balance_before=100_000,
                        balance_after=90_000,
                        created_at=now - timedelta(days=2),
                    ),
                    SupplierBalanceTransaction(
                        provider="lehai",
                        kind="refunded",
                        amount=-27_000,
                        balance_before=200_000,
                        balance_after=173_000,
                        created_at=now - timedelta(days=3),
                    ),
                ]
            )
            await session.commit()

        assert await pending_unresolved_supplier_alerts(
            sessions, provider="sumistore", now=now
        ) == ()
        assert await pending_unresolved_supplier_alerts(
            sessions, provider="lehai", now=now
        ) == ()
        await engine.dispose()

    asyncio.run(scenario())


def test_successful_telegram_alert_is_sent_once() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            session.add(
                SupplierBalanceTransaction(
                    provider="sumistore",
                    kind="suspicious",
                    amount=-20_000,
                    balance_before=100_000,
                    balance_after=80_000,
                    created_at=datetime.now(UTC) - timedelta(hours=25),
                )
            )
            await session.commit()

        bot = FakeBot()
        first = await notify_unresolved_supplier_alerts(
            sessions,
            bot,  # type: ignore[arg-type]
            (11, 22),
            provider="sumistore",
            provider_label="Sumi",
        )
        second = await notify_unresolved_supplier_alerts(
            sessions,
            bot,  # type: ignore[arg-type]
            (11, 22),
            provider="sumistore",
            provider_label="Sumi",
        )

        assert first == 1
        assert second == 0
        assert [chat_id for chat_id, _text in bot.messages] == [11, 22]
        assert all("Không thể tự thu hồi" in text for _chat_id, text in bot.messages)
        async with sessions() as session:
            transaction = await session.scalar(select(SupplierBalanceTransaction))
            assert transaction is not None and transaction.admin_alerted_at is not None
        await engine.dispose()

    asyncio.run(scenario())


def test_failed_telegram_alert_remains_retryable() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            session.add(
                SupplierBalanceTransaction(
                    provider="lehai",
                    kind="suspicious",
                    amount=-27_000,
                    balance_before=200_000,
                    balance_after=173_000,
                    created_at=datetime.now(UTC) - timedelta(hours=49),
                )
            )
            await session.commit()

        failed = await notify_unresolved_supplier_alerts(
            sessions,
            FakeBot(fail=True),  # type: ignore[arg-type]
            (11,),
            provider="lehai",
            provider_label="Lê Hải Premium",
        )
        succeeded_bot = FakeBot()
        retried = await notify_unresolved_supplier_alerts(
            sessions,
            succeeded_bot,  # type: ignore[arg-type]
            (11,),
            provider="lehai",
            provider_label="Lê Hải Premium",
        )

        assert failed == 0
        assert retried == 1
        assert len(succeeded_bot.messages) == 1
        await engine.dispose()

    asyncio.run(scenario())
