import asyncio
from datetime import UTC, datetime

from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.broadcasts import deliver_pending_stock_alerts
from app.database import Base
from app.models import Category, Product, ProductStockAlert, User
from app.stock_alerts import apply_supplier_stock


async def make_database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


class FakeStockBot:
    def __init__(self, failing_user_id: int | None = None) -> None:
        self.failing_user_id = failing_user_id
        self.calls: list[tuple[int, str, object]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.calls.append((chat_id, text, kwargs.get("reply_markup")))
        if chat_id == self.failing_user_id:
            raise TelegramForbiddenError(
                method=object(),  # type: ignore[arg-type]
                message="Forbidden: bot was blocked by the user",
            )


def test_stock_return_is_queued_once_and_sent_to_started_users() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus",
                name_en="GPT Plus",
                price=11_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GPT",
                external_stock=0,
                supplier_synced_at=datetime.now(UTC),
            )
            session.add(product)
            await session.flush()

            # The first successful sync establishes a baseline without a fake alert.
            assert await apply_supplier_stock(session, product, 5) is False
            product.external_stock = 5
            await session.commit()
            assert await session.scalar(select(ProductStockAlert.id)) is None

            assert await apply_supplier_stock(session, product, 0) is False
            product.external_stock = 0
            await session.commit()

            assert await apply_supplier_stock(session, product, 4) is True
            product.external_stock = 4
            await session.commit()

            # Repeated positive syncs update the pending event instead of spamming.
            assert await apply_supplier_stock(session, product, 6) is False
            product.external_stock = 6
            session.add_all(
                [
                    User(telegram_id=1, full_name="Vietnamese", language="vi", has_started=True),
                    User(telegram_id=2, full_name="English", language="en", has_started=True),
                    User(telegram_id=3, full_name="Blocked", language="vi", has_started=True),
                    User(telegram_id=4, full_name="Inactive", language="vi", has_started=False),
                ]
            )
            await session.commit()

        bot = FakeStockBot(failing_user_id=3)
        processed = await deliver_pending_stock_alerts(
            sessions,
            bot,  # type: ignore[arg-type]
            throttle_seconds=0,
        )
        assert processed == 1
        assert [call[0] for call in bot.calls] == [1, 2, 3]
        assert "HÀNG MỚI VỀ" in bot.calls[0][1]
        assert "Kho vừa có: <b>6</b>" in bot.calls[0][1]
        assert "PRODUCT BACK IN STOCK" in bot.calls[1][1]
        assert bot.calls[0][2].inline_keyboard[0][0].callback_data == f"prod:{product.id}"

        async with sessions() as session:
            alerts = list(
                await session.scalars(select(ProductStockAlert).order_by(ProductStockAlert.id))
            )
            blocked = await session.get(User, 3)
            assert len(alerts) == 1
            assert alerts[0].status == "sent"
            assert alerts[0].stock_before == 0
            assert alerts[0].stock_after == 6
            assert alerts[0].total_recipients == 3
            assert alerts[0].delivered_count == 2
            assert alerts[0].failed_count == 1
            assert blocked is not None and blocked.has_started is False
        await engine.dispose()

    asyncio.run(scenario())


def test_temporary_supplier_error_does_not_create_a_false_stock_alert() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Stable stock",
                name_en="Stable stock",
                price=20_000,
                fulfillment_source="sumistore",
                external_stock=8,
                supplier_available_stock=8,
                supplier_available_stock_initialized=True,
                supplier_synced_at=datetime.now(UTC),
            )
            session.add(product)
            await session.commit()

            # Supplier errors may temporarily hide external stock, but they do
            # not change the last successfully observed supplier stock.
            product.external_stock = 0
            await session.commit()
            assert await apply_supplier_stock(session, product, 8) is False
            product.external_stock = 8
            await session.commit()
            assert await session.scalar(select(ProductStockAlert.id)) is None
        await engine.dispose()

    asyncio.run(scenario())
