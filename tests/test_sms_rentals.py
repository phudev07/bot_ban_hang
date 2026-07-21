import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import BalanceAdjustment, ReferralReward, SmsRental, User, WalletTransaction
from app.rentsim import RentSimError, RentSimOtp, RentSimRental, RentSimSnapshot
from app.sms_rentals import (
    mark_sms_review_alerted,
    pending_sms_review_alerts,
    poll_pending_sms_rentals,
    refund_sms_rental,
    rent_sms_number,
)


class FakeRentSim:
    def __init__(self) -> None:
        self.balance_lock = asyncio.Lock()
        self.rent_count = 0
        self.otp_status = "pending"
        self.rent_error: str | None = None
        self.balance_after = 49_000

    async def fetch_snapshot(self, *, force: bool = False) -> RentSimSnapshot:
        assert force is True
        return RentSimSnapshot(
            service_id="chatgpt",
            service_name="ChatGPT",
            server_id="kh2",
            unit_price=1_000,
            source_stock=50,
            balance=50_000,
        )

    async def rent(self) -> RentSimRental:
        self.rent_count += 1
        await asyncio.sleep(0.01)
        if self.rent_error:
            raise RentSimError(self.rent_error)
        return RentSimRental(
            order_id=f"ORDER-{self.rent_count}",
            status="pending",
            phone_number=f"+85500000{self.rent_count}",
            phone_number_display=f"000 00{self.rent_count}",
            country_code="+855",
            service_name="ChatGPT",
        )

    async def fetch_balance(self) -> int:
        return self.balance_after

    async def fetch_otp(self, order_id: str) -> RentSimOtp:
        if self.otp_status == "success":
            return RentSimOtp(
                status="success",
                order_id=order_id,
                service_name="ChatGPT",
                code="654321",
                content="654321 is your ChatGPT verification code.",
            )
        return RentSimOtp(status=self.otp_status, order_id=order_id)


async def make_database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_sms_rental_charges_wallet_enforces_cooldown_and_unlocks_after_otp() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        client = FakeRentSim()
        started = datetime.now(UTC)
        async with sessions() as session:
            referrer = User(telegram_id=1001, full_name="Referrer", balance=0)
            buyer = User(
                telegram_id=1002,
                full_name="Buyer",
                balance=10_000,
                referred_by_id=referrer.telegram_id,
            )
            session.add_all([referrer, buyer])
            await session.commit()

        first, simultaneous = await asyncio.gather(
            rent_sms_number(
                sessions,
                1002,
                client,  # type: ignore[arg-type]
                now=started,
            ),
            rent_sms_number(
                sessions,
                1002,
                client,  # type: ignore[arg-type]
                now=started,
            ),
        )
        results = {first.message: first, simultaneous.message: simultaneous}
        assert results["pending"].ok is True
        assert results["cooldown"].retry_after >= 60
        assert client.rent_count == 1

        client.otp_status = "success"
        notifications = await poll_pending_sms_rentals(
            sessions,
            client,  # type: ignore[arg-type]
            now=started + timedelta(seconds=6),
        )
        assert len(notifications) == 1
        assert notifications[0].status == "success"
        assert notifications[0].otp_code == "654321"

        blocked = await rent_sms_number(
            sessions,
            1002,
            client,  # type: ignore[arg-type]
            now=started + timedelta(seconds=10),
        )
        assert blocked.ok is False and blocked.message == "cooldown"

        next_rental = await rent_sms_number(
            sessions,
            1002,
            client,  # type: ignore[arg-type]
            now=started + timedelta(seconds=61),
        )
        assert next_rental.ok is True
        assert client.rent_count == 2

        async with sessions() as session:
            buyer = await session.get(User, 1002)
            referrer = await session.get(User, 1001)
            reward = await session.scalar(select(ReferralReward))
            rentals = list(await session.scalars(select(SmsRental).order_by(SmsRental.id)))
            assert buyer is not None and buyer.balance == 6_000
            assert referrer is not None and referrer.balance == 100
            assert reward is not None and reward.shop_order_code == rentals[0].shop_order_code
            assert [rental.status for rental in rentals] == ["success", "pending"]
        await engine.dispose()

    asyncio.run(scenario())


def test_sms_timeout_refunds_wallet_exactly_once() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        client = FakeRentSim()
        started = datetime.now(UTC)
        async with sessions() as session:
            session.add(User(telegram_id=2001, full_name="Buyer", balance=5_000))
            await session.commit()

        rented = await rent_sms_number(
            sessions,
            2001,
            client,  # type: ignore[arg-type]
            now=started,
        )
        assert rented.ok is True
        client.otp_status = "timeout"
        first = await poll_pending_sms_rentals(
            sessions,
            client,  # type: ignore[arg-type]
            now=started + timedelta(seconds=6),
        )
        second = await poll_pending_sms_rentals(
            sessions,
            client,  # type: ignore[arg-type]
            now=started + timedelta(seconds=12),
        )

        assert len(first) == 1 and first[0].status == "refunded"
        assert second == []
        async with sessions() as session:
            user = await session.get(User, 2001)
            rental = await session.scalar(select(SmsRental))
            adjustments = int(
                await session.scalar(select(func.count(BalanceAdjustment.id))) or 0
            )
            wallet_transactions = list(
                await session.scalars(select(WalletTransaction).order_by(WalletTransaction.id))
            )
            assert user is not None and user.balance == 5_000
            assert rental is not None and rental.status == "refunded"
            assert rental.failure_reason == "otp_timeout"
            assert adjustments == 1
            assert [item.kind for item in wallet_transactions] == [
                "sms_rental",
                "sms_refund",
            ]
            assert [item.amount for item in wallet_transactions] == [-2_000, 2_000]
            assert [item.balance_before for item in wallet_transactions] == [5_000, 3_000]
            assert [item.balance_after for item in wallet_transactions] == [3_000, 5_000]
        await engine.dispose()

    asyncio.run(scenario())


def test_manual_provider_refund_is_idempotent() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            session.add(
                User(telegram_id=2051, full_name="Buyer", balance=3_000)
            )
            session.add(
                SmsRental(
                    user_id=2051,
                    shop_order_code="SMS-MANUAL",
                    provider_order_id="PROVIDER-REFUNDED",
                    status="pending",
                    sale_amount=2_000,
                    cost_amount=1_000,
                )
            )
            await session.commit()

        first = await refund_sms_rental(sessions, 1, reason="provider_failed")
        second = await refund_sms_rental(sessions, 1, reason="provider_failed")
        assert first is not None and first.status == "refunded"
        assert second is None
        async with sessions() as session:
            user = await session.get(User, 2051)
            adjustments = int(
                await session.scalar(select(func.count(BalanceAdjustment.id))) or 0
            )
            assert user is not None and user.balance == 5_000
            assert adjustments == 1
        await engine.dispose()

    asyncio.run(scenario())


def test_pending_rental_allows_another_number_after_sixty_seconds() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        client = FakeRentSim()
        started = datetime.now(UTC)
        async with sessions() as session:
            session.add(User(telegram_id=2101, full_name="Buyer", balance=10_000))
            await session.commit()

        first = await rent_sms_number(
            sessions,
            2101,
            client,  # type: ignore[arg-type]
            now=started,
        )
        second = await rent_sms_number(
            sessions,
            2101,
            client,  # type: ignore[arg-type]
            now=started + timedelta(seconds=61),
        )

        assert first.ok is True and first.status == "pending"
        assert second.ok is True and second.status == "pending"
        assert client.rent_count == 2
        async with sessions() as session:
            user = await session.get(User, 2101)
            assert user is not None and user.balance == 6_000
        await engine.dispose()

    asyncio.run(scenario())


def test_sms_provider_failure_refunds_reserved_wallet_balance() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        client = FakeRentSim()
        client.rent_error = "OUT_OF_STOCK"
        async with sessions() as session:
            session.add(User(telegram_id=3001, full_name="Buyer", balance=5_000))
            await session.commit()

        result = await rent_sms_number(
            sessions,
            3001,
            client,  # type: ignore[arg-type]
        )
        assert result.ok is False
        assert result.message == "out_of_stock"
        assert result.status == "refunded"
        async with sessions() as session:
            user = await session.get(User, 3001)
            rental = await session.scalar(select(SmsRental))
            assert user is not None and user.balance == 5_000
            assert rental is not None and rental.status == "refunded"
        await engine.dispose()

    asyncio.run(scenario())


def test_ambiguous_provider_failure_holds_balance_for_review() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        client = FakeRentSim()
        client.rent_error = "PROVIDER_UNAVAILABLE"
        async with sessions() as session:
            session.add(User(telegram_id=3051, full_name="Buyer", balance=5_000))
            await session.commit()

        result = await rent_sms_number(
            sessions,
            3051,
            client,  # type: ignore[arg-type]
        )
        assert result.ok is False
        assert result.message == "provider_result_unknown"
        assert result.status == "unknown"
        assert result.provider_balance_before == 50_000
        assert result.provider_balance_after == 49_000

        blocked = await rent_sms_number(
            sessions,
            3051,
            client,  # type: ignore[arg-type]
            now=datetime.now(UTC) + timedelta(minutes=10),
        )
        assert blocked.message == "provider_result_unknown"
        assert client.rent_count == 1
        async with sessions() as session:
            user = await session.get(User, 3051)
            rental = await session.scalar(select(SmsRental))
            adjustments = int(
                await session.scalar(select(func.count(BalanceAdjustment.id))) or 0
            )
            assert user is not None and user.balance == 3_000
            assert rental is not None and rental.status == "unknown"
            assert rental.provider_balance_after == 49_000
            assert adjustments == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_http_500_without_provider_order_refunds_and_allows_retry() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        client = FakeRentSim()
        client.rent_error = "PROVIDER_HTTP_500"
        started = datetime.now(UTC)
        async with sessions() as session:
            session.add(User(telegram_id=3061, full_name="Buyer", balance=5_000))
            await session.commit()

        failed = await rent_sms_number(
            sessions,
            3061,
            client,  # type: ignore[arg-type]
            now=started,
        )
        assert failed.ok is False
        assert failed.message == "provider_error_refunded"
        assert failed.status == "refunded"
        assert failed.provider_balance_before == 50_000
        assert failed.provider_balance_after is None

        client.rent_error = None
        cooldown = await rent_sms_number(
            sessions,
            3061,
            client,  # type: ignore[arg-type]
            now=started + timedelta(seconds=1),
        )
        assert cooldown.ok is False and cooldown.message == "cooldown"
        assert client.rent_count == 1

        retried = await rent_sms_number(
            sessions,
            3061,
            client,  # type: ignore[arg-type]
            now=started + timedelta(seconds=61),
        )
        assert retried.ok is True
        assert retried.status == "pending"
        assert client.rent_count == 2
        async with sessions() as session:
            user = await session.get(User, 3061)
            rentals = list(await session.scalars(select(SmsRental).order_by(SmsRental.id)))
            assert user is not None and user.balance == 3_000
            assert [rental.status for rental in rentals] == ["refunded", "pending"]
        await engine.dispose()

    asyncio.run(scenario())


def test_simultaneous_http_500_rentals_refund_each_user_once() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        client = FakeRentSim()
        client.rent_error = "PROVIDER_HTTP_500"
        async with sessions() as session:
            session.add_all(
                [
                    User(telegram_id=3062, full_name="Buyer 1", balance=5_000),
                    User(telegram_id=3063, full_name="Buyer 2", balance=5_000),
                ]
            )
            await session.commit()

        first, second = await asyncio.gather(
            rent_sms_number(sessions, 3062, client),  # type: ignore[arg-type]
            rent_sms_number(sessions, 3063, client),  # type: ignore[arg-type]
        )
        assert first.status == "refunded" and second.status == "refunded"
        assert first.message == "provider_error_refunded"
        assert second.message == "provider_error_refunded"
        assert client.rent_count == 2
        async with sessions() as session:
            users = list(await session.scalars(select(User).order_by(User.telegram_id)))
            rentals = list(await session.scalars(select(SmsRental).order_by(SmsRental.id)))
            adjustments = int(
                await session.scalar(select(func.count(BalanceAdjustment.id))) or 0
            )
            assert [user.balance for user in users] == [5_000, 5_000]
            assert [rental.status for rental in rentals] == ["refunded", "refunded"]
            assert adjustments == 2
        await engine.dispose()

    asyncio.run(scenario())


def test_old_unknown_request_with_unchanged_balance_is_auto_refunded() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        client = FakeRentSim()
        client.balance_after = 89_000
        started = datetime.now(UTC) - timedelta(seconds=61)
        async with sessions() as session:
            user = User(telegram_id=3071, full_name="Buyer", balance=3_000)
            session.add(user)
            await session.flush()
            session.add(
                SmsRental(
                    user_id=user.telegram_id,
                    shop_order_code="SMS15",
                    status="unknown",
                    sale_amount=2_000,
                    cost_amount=1_000,
                    provider_balance_before=89_000,
                    provider_balance_after=89_000,
                    last_error="provider_http_500",
                    requested_at=started,
                )
            )
            await session.commit()

        notifications = await poll_pending_sms_rentals(
            sessions,
            client,  # type: ignore[arg-type]
            now=datetime.now(UTC),
        )
        assert len(notifications) == 1
        assert notifications[0].status == "refunded"
        assert notifications[0].failure_reason == "provider_request_not_confirmed"
        async with sessions() as session:
            user = await session.get(User, 3071)
            rental = await session.scalar(select(SmsRental))
            assert user is not None and user.balance == 5_000
            assert rental is not None and rental.status == "refunded"
        await engine.dispose()

    asyncio.run(scenario())


def test_sms_rental_requires_wallet_balance_before_calling_provider() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        client = FakeRentSim()
        async with sessions() as session:
            session.add(User(telegram_id=3101, full_name="Buyer", balance=1_999))
            await session.commit()

        result = await rent_sms_number(
            sessions,
            3101,
            client,  # type: ignore[arg-type]
        )

        assert result.ok is False
        assert result.message == "insufficient"
        assert result.sale_amount == 2_000
        assert client.rent_count == 0
        async with sessions() as session:
            rental_count = int(await session.scalar(select(func.count(SmsRental.id))) or 0)
            user = await session.get(User, 3101)
            assert rental_count == 0
            assert user is not None and user.balance == 1_999
        await engine.dispose()

    asyncio.run(scenario())


def test_stale_request_is_held_for_review_after_process_interruption() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        client = FakeRentSim()
        now = datetime.now(UTC)
        async with sessions() as session:
            user = User(telegram_id=4001, full_name="Buyer", balance=3_000)
            rental = SmsRental(
                user_id=user.telegram_id,
                shop_order_code="SMS-STALE",
                status="requesting",
                sale_amount=2_000,
                cost_amount=1_000,
                requested_at=now - timedelta(minutes=3),
            )
            session.add_all([user, rental])
            await session.commit()

        notifications = await poll_pending_sms_rentals(
            sessions,
            client,  # type: ignore[arg-type]
            request_recovery_seconds=120,
            now=now,
        )

        assert len(notifications) == 1
        assert notifications[0].status == "unknown"
        async with sessions() as session:
            user = await session.get(User, 4001)
            rental = await session.scalar(select(SmsRental))
            assert user is not None and user.balance == 3_000
            assert rental is not None and rental.failure_reason == "stale_request_review"
        await engine.dispose()

    asyncio.run(scenario())


def test_unknown_and_old_pending_rentals_alert_admin_once() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        now = datetime.now(UTC)
        async with sessions() as session:
            user = User(telegram_id=5001, full_name="Buyer", balance=3_000)
            session.add(user)
            session.add_all(
                [
                    SmsRental(
                        user_id=user.telegram_id,
                        shop_order_code="SMS-UNKNOWN",
                        status="unknown",
                        sale_amount=2_000,
                        cost_amount=1_000,
                        requested_at=now,
                    ),
                    SmsRental(
                        user_id=user.telegram_id,
                        shop_order_code="SMS-OLD-PENDING",
                        provider_order_id="ORDER-OLD",
                        phone_number="+85511111111",
                        status="pending",
                        sale_amount=2_000,
                        cost_amount=1_000,
                        requested_at=now - timedelta(minutes=10),
                    ),
                    SmsRental(
                        user_id=user.telegram_id,
                        shop_order_code="SMS-RECENT",
                        provider_order_id="ORDER-RECENT",
                        phone_number="+85522222222",
                        status="pending",
                        sale_amount=2_000,
                        cost_amount=1_000,
                        requested_at=now - timedelta(seconds=30),
                    ),
                ]
            )
            await session.commit()

        alerts = await pending_sms_review_alerts(
            sessions,
            pending_alert_seconds=300,
            now=now,
        )
        assert {alert.shop_order_code for alert in alerts} == {
            "SMS-UNKNOWN",
            "SMS-OLD-PENDING",
        }
        unknown = next(alert for alert in alerts if alert.status == "unknown")
        assert await mark_sms_review_alerted(sessions, unknown.rental_id, now=now) is True
        remaining = await pending_sms_review_alerts(
            sessions,
            pending_alert_seconds=300,
            now=now,
        )
        assert [alert.shop_order_code for alert in remaining] == ["SMS-OLD-PENDING"]
        await engine.dispose()

    asyncio.run(scenario())
