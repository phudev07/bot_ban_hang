import asyncio
from types import SimpleNamespace

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.admin import create_admin_router
from app.broadcasts import (
    BroadcastRateLimiter,
    deliver_broadcast,
    deliver_queued_broadcasts,
    queue_broadcast,
    recover_interrupted_broadcasts,
)
from app.config import Settings
from app.database import Base
from app.models import BroadcastDelivery, BroadcastLog, User
from app.states import BroadcastStates
from app.utils import SecretCipher


async def make_database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


class FakeBot:
    def __init__(self, failing_user_id: int | None = None) -> None:
        self.failing_user_id = failing_user_id
        self.copy_calls: list[int] = []
        self.copy_kwargs: list[dict[str, object]] = []
        self.copy_markups = []

    async def copy_message(self, *, chat_id: int, **kwargs) -> None:
        self.copy_calls.append(chat_id)
        self.copy_kwargs.append(kwargs)
        self.copy_markups.append(kwargs.get("reply_markup"))
        if chat_id == self.failing_user_id:
            raise TelegramForbiddenError(
                method=object(),  # type: ignore[arg-type]
                message="Forbidden: bot was blocked by the user",
            )


class ConcurrentFakeBot(FakeBot):
    def __init__(self, failing_user_id: int | None = None) -> None:
        super().__init__(failing_user_id)
        self.active = 0
        self.max_active = 0

    async def copy_message(self, *, chat_id: int, **kwargs) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.003)
            await super().copy_message(chat_id=chat_id, **kwargs)
        finally:
            self.active -= 1


class RetryOnceBot(FakeBot):
    def __init__(self) -> None:
        super().__init__()
        self.retried = False

    async def copy_message(self, *, chat_id: int, **kwargs) -> None:
        if not self.retried:
            self.retried = True
            self.copy_calls.append(chat_id)
            raise TelegramRetryAfter(
                method=object(),  # type: ignore[arg-type]
                message="Too Many Requests",
                retry_after=0,
            )
        await super().copy_message(chat_id=chat_id, **kwargs)


class FakeState:
    def __init__(self) -> None:
        self.current = None
        self.data: dict[str, int] = {}

    async def clear(self) -> None:
        self.current = None
        self.data = {}

    async def set_state(self, state) -> None:
        self.current = state

    async def update_data(self, **values: int) -> None:
        self.data.update(values)

    async def get_data(self) -> dict[str, int]:
        return self.data


class FakeMessage:
    def __init__(self, *, message_id: int, reply_to_message=None) -> None:
        self.from_user = SimpleNamespace(id=42)
        self.chat = SimpleNamespace(id=42)
        self.message_id = message_id
        self.reply_to_message = reply_to_message
        self.answers: list[tuple[str, dict[str, object]]] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append((text, kwargs))

    async def edit_reply_markup(self, **_kwargs) -> None:
        return None


class FakeCallback:
    def __init__(self, message: FakeMessage) -> None:
        self.from_user = SimpleNamespace(id=42)
        self.message = message
        self.answers: list[tuple[str, dict[str, object]]] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append((text, kwargs))


def test_broadcast_requires_confirmation_before_delivery() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            session.add_all(
                [
                    User(telegram_id=1, full_name="Started", has_started=True),
                    User(telegram_id=2, full_name="Not started", has_started=False),
                ]
            )
            await session.commit()

            settings = Settings(
                _env_file=None,
                bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
                inventory_encryption_key=Fernet.generate_key().decode(),
                sepay_enabled=False,
                ADMIN_IDS="42",
            )
            router = create_admin_router(
                settings,
                SecretCipher(settings.inventory_encryption_key.get_secret_value()),
            )
            begin = next(
                handler.callback
                for handler in router.message.handlers
                if handler.callback.__name__ == "begin_broadcast"
            )

            waiting_state = FakeState()
            waiting_message = FakeMessage(message_id=10)
            bot = FakeBot()
            await begin(waiting_message, bot, session, waiting_state)
            assert waiting_state.current == BroadcastStates.waiting_for_content
            assert "Gửi tin nhắn" in waiting_message.answers[-1][0]

            source = FakeMessage(message_id=20)
            confirmation_state = FakeState()
            command = FakeMessage(message_id=21, reply_to_message=source)
            await begin(command, bot, session, confirmation_state)
            assert confirmation_state.current == BroadcastStates.waiting_for_confirmation
            assert confirmation_state.data == {
                "source_chat_id": 42,
                "source_message_id": 20,
                "recipient_count": 1,
            }
            assert bot.copy_calls == [42]
            assert bot.copy_kwargs[-1]["from_chat_id"] == 42
            assert bot.copy_kwargs[-1]["message_id"] == 20
            markup = bot.copy_markups[-1]
            assert markup.inline_keyboard[0][0].callback_data == "broadcast:confirm"
        await engine.dispose()

    asyncio.run(scenario())


def test_broadcast_photo_gets_confirmation_button_on_preview() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            session.add(User(telegram_id=1, full_name="Started", has_started=True))
            await session.commit()

            settings = Settings(
                _env_file=None,
                bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
                inventory_encryption_key=Fernet.generate_key().decode(),
                sepay_enabled=False,
                ADMIN_IDS="42",
            )
            router = create_admin_router(
                settings,
                SecretCipher(settings.inventory_encryption_key.get_secret_value()),
            )
            receive = next(
                handler.callback
                for handler in router.message.handlers
                if handler.callback.__name__ == "receive_broadcast_content"
            )
            source = FakeMessage(message_id=31)
            source.photo = [SimpleNamespace(file_id="photo-file")]
            state = FakeState()
            bot = FakeBot()

            await receive(source, bot, session, state)

            assert state.current == BroadcastStates.waiting_for_confirmation
            assert state.data["source_message_id"] == 31
            assert bot.copy_calls == [42]
            markup = bot.copy_markups[0]
            assert markup.inline_keyboard[0][0].callback_data == "broadcast:confirm"
            assert "Gửi tới 1 người" in markup.inline_keyboard[0][0].text
        await engine.dispose()

    asyncio.run(scenario())


def test_broadcast_confirmation_queues_without_blocking_on_delivery() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            session.add_all(
                [
                    User(telegram_id=1, full_name="One", has_started=True),
                    User(telegram_id=2, full_name="Two", has_started=True),
                ]
            )
            await session.commit()

        settings = Settings(
            _env_file=None,
            bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
            inventory_encryption_key=Fernet.generate_key().decode(),
            sepay_enabled=False,
            ADMIN_IDS="42",
        )
        router = create_admin_router(
            settings,
            SecretCipher(settings.inventory_encryption_key.get_secret_value()),
        )
        confirm = next(
            handler.callback
            for handler in router.callback_query.handlers
            if handler.callback.__name__ == "confirm_broadcast"
        )
        state = FakeState()
        state.data = {
            "source_chat_id": 42,
            "source_message_id": 90,
            "recipient_count": 2,
        }
        message = FakeMessage(message_id=91)
        callback = FakeCallback(message)

        await confirm(callback, sessions, state)

        assert callback.answers[-1][0] == "Đã đưa vào hàng chờ."
        assert "Đã đưa thông báo vào hàng chờ" in message.answers[-1][0]
        async with sessions() as session:
            campaign = await session.scalar(select(BroadcastLog))
            deliveries = list(await session.scalars(select(BroadcastDelivery)))
            assert campaign is not None and campaign.status == "queued"
            assert campaign.total_recipients == 2
            assert len(deliveries) == 2
            assert all(delivery.status == "pending" for delivery in deliveries)
        await engine.dispose()

    asyncio.run(scenario())


def test_broadcast_delivers_only_to_started_users_and_logs_result() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            session.add_all(
                [
                    User(telegram_id=1, full_name="Delivered", has_started=True),
                    User(telegram_id=2, full_name="Blocked", has_started=True),
                    User(telegram_id=3, full_name="Inactive", has_started=False),
                ]
            )
            await session.commit()

            bot = FakeBot(failing_user_id=2)
            result = await deliver_broadcast(
                session,
                bot,  # type: ignore[arg-type]
                admin_id=42,
                source_chat_id=42,
                source_message_id=99,
                throttle_seconds=0,
            )
            assert result.total == 2
            assert result.delivered == 1
            assert result.failed == 1
            assert bot.copy_calls == [1, 2]
            assert all(
                markup.inline_keyboard[0][0].callback_data == "menu:products"
                and markup.inline_keyboard[0][0].text == "🛒 Mua ngay"
                for markup in bot.copy_markups
            )

            log = await session.scalar(select(BroadcastLog))
            blocked_user = await session.get(User, 2)
            assert log is not None
            assert log.total_recipients == 2
            assert log.delivered_count == 1
            assert log.failed_count == 1
            assert log.status == "completed"
            assert blocked_user is not None and blocked_user.has_started is False
        await engine.dispose()

    asyncio.run(scenario())


def test_queued_broadcast_sends_concurrently_and_persists_each_recipient() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            session.add_all(
                User(
                    telegram_id=10_000 + index,
                    full_name=f"Recipient {index}",
                    has_started=True,
                )
                for index in range(205)
            )
            await session.commit()

        queued = await queue_broadcast(
            sessions,
            admin_id=42,
            source_chat_id=42,
            source_message_id=99,
        )
        assert queued.total == 205

        failing_user_id = 10_017
        bot = ConcurrentFakeBot(failing_user_id=failing_user_id)
        processed = await deliver_queued_broadcasts(
            sessions,
            bot,  # type: ignore[arg-type]
            BroadcastRateLimiter(10_000),
            concurrency=12,
            batch_size=100,
        )

        assert processed == 1
        assert len(bot.copy_calls) == 205
        assert 1 < bot.max_active <= 12
        async with sessions() as session:
            campaign = await session.get(BroadcastLog, queued.broadcast_id)
            statuses = dict(
                (
                    await session.execute(
                    select(BroadcastDelivery.user_id, BroadcastDelivery.status).where(
                        BroadcastDelivery.broadcast_id == queued.broadcast_id
                    )
                    )
                ).all()
            )
            failed_user = await session.get(User, failing_user_id)
            assert campaign is not None and campaign.status == "completed"
            assert campaign.delivered_count == 204
            assert campaign.failed_count == 1
            assert campaign.started_at is not None
            assert campaign.completed_at is not None
            assert statuses[failing_user_id] == "failed"
            assert sum(status == "sent" for status in statuses.values()) == 204
            assert failed_user is not None and failed_user.has_started is False
        await engine.dispose()

    asyncio.run(scenario())


def test_interrupted_broadcast_resumes_without_resending_completed_users() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            session.add_all(
                [
                    User(telegram_id=1, full_name="Done", has_started=True),
                    User(telegram_id=2, full_name="Interrupted", has_started=True),
                    User(telegram_id=3, full_name="Pending", has_started=True),
                ]
            )
            await session.commit()

        queued = await queue_broadcast(
            sessions,
            admin_id=42,
            source_chat_id=42,
            source_message_id=77,
        )
        async with sessions() as session:
            campaign = await session.get(BroadcastLog, queued.broadcast_id)
            deliveries = list(
                await session.scalars(
                    select(BroadcastDelivery)
                    .where(BroadcastDelivery.broadcast_id == queued.broadcast_id)
                    .order_by(BroadcastDelivery.user_id)
                )
            )
            assert campaign is not None
            campaign.status = "sending"
            deliveries[0].status = "sent"
            deliveries[1].status = "sending"
            await session.commit()

        await recover_interrupted_broadcasts(sessions)
        bot = FakeBot()
        await deliver_queued_broadcasts(
            sessions,
            bot,  # type: ignore[arg-type]
            BroadcastRateLimiter(10_000),
            concurrency=4,
            batch_size=100,
        )

        assert sorted(bot.copy_calls) == [2, 3]
        async with sessions() as session:
            campaign = await session.get(BroadcastLog, queued.broadcast_id)
            assert campaign is not None and campaign.status == "completed"
            assert campaign.delivered_count == 3
            assert campaign.failed_count == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_queued_broadcast_retries_telegram_rate_limit() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            session.add(User(telegram_id=1, full_name="Retry", has_started=True))
            await session.commit()

        queued = await queue_broadcast(
            sessions,
            admin_id=42,
            source_chat_id=42,
            source_message_id=77,
        )
        bot = RetryOnceBot()
        await deliver_queued_broadcasts(
            sessions,
            bot,  # type: ignore[arg-type]
            BroadcastRateLimiter(10_000),
            concurrency=2,
            batch_size=100,
        )

        assert bot.copy_calls == [1, 1]
        async with sessions() as session:
            campaign = await session.get(BroadcastLog, queued.broadcast_id)
            assert campaign is not None and campaign.status == "completed"
            assert campaign.delivered_count == 1
            assert campaign.failed_count == 0
        await engine.dispose()

    asyncio.run(scenario())
