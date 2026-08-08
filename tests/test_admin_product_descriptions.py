import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from aiogram.enums import ChatType, MessageEntityType
from aiogram.types import Chat, Message, MessageEntity, User as TelegramUser
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.admin import custom_emoji_ids, create_admin_router
from app.config import Settings
from app.database import Base
from app.models import Category, Product
from app.states import ProductDescriptionStates
from app.utils import SecretCipher


class FakeState:
    def __init__(self) -> None:
        self.current = None
        self.data: dict[str, object] = {}

    async def clear(self) -> None:
        self.current = None
        self.data = {}

    async def set_state(self, state) -> None:
        self.current = state

    async def update_data(self, **values: object) -> None:
        self.data.update(values)

    async def get_data(self) -> dict[str, object]:
        return self.data


class FakeMessage:
    def __init__(self, text: str = "", html_text: str | None = None) -> None:
        self.from_user = SimpleNamespace(id=42)
        self.chat = SimpleNamespace(id=42)
        self.message_id = 1
        self.text = text
        self.caption = None
        self.html_text = html_text if html_text is not None else text
        self.answers: list[tuple[str, dict[str, object]]] = []

    async def answer(self, text: str, **kwargs: object) -> None:
        self.answers.append((text, kwargs))

    async def edit_reply_markup(self, **_kwargs: object) -> None:
        return None


class FakeCallback:
    def __init__(self, message: FakeMessage, data: str) -> None:
        self.from_user = SimpleNamespace(id=42)
        self.message = message
        self.data = data
        self.answers: list[tuple[str, dict[str, object]]] = []

    async def answer(self, text: str = "", **kwargs: object) -> None:
        self.answers.append((text, kwargs))


def test_custom_emoji_ids_extracts_and_deduplicates_telegram_entities() -> None:
    emoji_id = "5310259124817134249"
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=42, type=ChatType.PRIVATE),
        from_user=TelegramUser(id=42, is_bot=False, first_name="Admin"),
        text="🤖🤖",
        entities=[
            MessageEntity(
                type=MessageEntityType.CUSTOM_EMOJI,
                offset=0,
                length=2,
                custom_emoji_id=emoji_id,
            ),
            MessageEntity(
                type=MessageEntityType.CUSTOM_EMOJI,
                offset=2,
                length=2,
                custom_emoji_id=emoji_id,
            ),
        ],
    )

    assert custom_emoji_ids(message) == [emoji_id]


def test_admin_sends_formatted_telegram_description_with_custom_emoji(tmp_path) -> None:
    async def scenario() -> None:
        database_path = (tmp_path / "telegram-product-description.db").as_posix()
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            visible = Product(
                category_id=category.id,
                name_vi="GPT Plus",
                name_en="GPT Plus",
                price=40_000,
                fulfillment_source="local",
                active=True,
            )
            hidden = Product(
                category_id=category.id,
                name_vi="Sản phẩm đang ẩn",
                name_en="Hidden product",
                price=40_000,
                fulfillment_source="local",
                active=False,
            )
            session.add_all([visible, hidden])
            await session.commit()

            settings = Settings(
                _env_file=None,
                bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
                inventory_encryption_key=Fernet.generate_key().decode(),
                ADMIN_IDS="42",
            )
            router = create_admin_router(
                settings,
                SecretCipher(settings.inventory_encryption_key.get_secret_value()),
            )
            begin = next(
                handler.callback
                for handler in router.message.handlers
                if handler.callback.__name__ == "begin_product_description"
            )
            select_language = next(
                handler.callback
                for handler in router.callback_query.handlers
                if handler.callback.__name__ == "select_description_language"
            )
            receive = next(
                handler.callback
                for handler in router.message.handlers
                if handler.callback.__name__ == "receive_product_description"
            )

            state = FakeState()
            command = FakeMessage("/mota")
            await begin(command, session, state)
            product_markup = command.answers[-1][1]["reply_markup"]
            product_callbacks = {
                button.callback_data
                for row in product_markup.inline_keyboard
                for button in row
            }
            assert f"admin:description:select:{visible.id}" in product_callbacks
            assert f"admin:description:select:{hidden.id}" not in product_callbacks

            language_callback = FakeCallback(
                FakeMessage(),
                f"admin:description:lang:{visible.id}:vi",
            )
            await select_language(language_callback, session, state)
            assert state.current == ProductDescriptionStates.waiting_for_content
            assert state.data == {"product_id": visible.id, "language": "vi"}

            description_html = (
                '<tg-emoji emoji-id="5312241539987020022">🔥</tg-emoji> '
                '<b>Ưu đãi Premium</b>\n<i>Giao tự động</i>'
            )
            description = FakeMessage(
                "🔥 Ưu đãi Premium\nGiao tự động",
                html_text=description_html,
            )
            await receive(description, session, state)

            await session.refresh(visible)
            assert visible.description_vi == description_html
            assert state.current is None
            assert "Đã cập nhật mô tả trong bot" in description.answers[-1][0]
            assert description_html in description.answers[-1][0]

        await engine.dispose()

    asyncio.run(scenario())
