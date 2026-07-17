import asyncio
from types import SimpleNamespace

from aiogram.exceptions import TelegramForbiddenError
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.admin import create_admin_router
from app.broadcasts import deliver_broadcast
from app.config import Settings
from app.database import Base
from app.models import BroadcastLog, User
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
        self.copy_markups = []

    async def copy_message(self, *, chat_id: int, **kwargs) -> None:
        self.copy_calls.append(chat_id)
        self.copy_markups.append(kwargs.get("reply_markup"))
        if chat_id == self.failing_user_id:
            raise TelegramForbiddenError(
                method=object(),  # type: ignore[arg-type]
                message="Forbidden: bot was blocked by the user",
            )


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


class FakeMessage:
    def __init__(self, *, message_id: int, reply_to_message=None) -> None:
        self.from_user = SimpleNamespace(id=42)
        self.chat = SimpleNamespace(id=42)
        self.message_id = message_id
        self.reply_to_message = reply_to_message
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
            await begin(waiting_message, session, waiting_state)
            assert waiting_state.current == BroadcastStates.waiting_for_content
            assert "Gửi tin nhắn" in waiting_message.answers[-1][0]

            source = FakeMessage(message_id=20)
            confirmation_state = FakeState()
            command = FakeMessage(message_id=21, reply_to_message=source)
            await begin(command, session, confirmation_state)
            assert confirmation_state.current == BroadcastStates.waiting_for_confirmation
            assert confirmation_state.data == {
                "source_chat_id": 42,
                "source_message_id": 20,
                "recipient_count": 1,
            }
            markup = command.answers[-1][1]["reply_markup"]
            assert markup.inline_keyboard[0][0].callback_data == "broadcast:confirm"
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
            assert blocked_user is not None and blocked_user.has_started is False
        await engine.dispose()

    asyncio.run(scenario())
