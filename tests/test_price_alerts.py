import asyncio
from datetime import UTC, datetime

from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.broadcasts import deliver_pending_sale_alerts, recover_interrupted_product_alerts
from app.database import Base
from app.models import Category, Product, ProductAlertDelivery, ProductPriceAlert, User
from app.price_alerts import apply_supplier_price


async def make_database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


class FakeSaleBot:
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


def test_each_supplier_drop_is_queued_again_after_a_price_increase() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tài khoản API",
                name_en="API account",
                price=17_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-TEST",
                supplier_markup=2_000,
                supplier_price=15_000,
                external_stock=10,
                supplier_synced_at=datetime.now(UTC),
            )
            session.add(product)
            await session.commit()

            assert await apply_supplier_price(session, product, 12_000) is True
            await session.commit()
            first = await session.scalar(select(ProductPriceAlert))
            assert first is not None
            assert first.sale_price_before == 17_000
            assert first.sale_price_after == 14_000
            first.status = "sent"
            first.sent_at = datetime.now(UTC)
            await session.commit()

            assert await apply_supplier_price(session, product, 10_000) is True
            await session.commit()
            second = await session.scalar(
                select(ProductPriceAlert).order_by(ProductPriceAlert.id.desc()).limit(1)
            )
            assert second is not None and second.id != first.id
            assert second.sale_price_before == 14_000
            assert second.sale_price_after == 12_000
            second.status = "sent"
            second.sent_at = datetime.now(UTC)
            await session.commit()

            assert await apply_supplier_price(session, product, 15_000) is False
            await session.commit()
            assert product.price == 17_000

            assert await apply_supplier_price(session, product, 12_000) is True
            await session.commit()
            alerts = list(
                await session.scalars(
                    select(ProductPriceAlert).order_by(ProductPriceAlert.id)
                )
            )
            assert len(alerts) == 3
            assert alerts[-1].sale_price_before == 17_000
            assert alerts[-1].sale_price_after == 14_000
        await engine.dispose()

    asyncio.run(scenario())


def test_first_supplier_sync_does_not_create_a_fake_sale() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Hàng mới",
                name_en="New item",
                price=17_000,
                fulfillment_source="sumistore",
                supplier_markup=2_000,
                supplier_price=15_000,
                supplier_synced_at=None,
            )
            session.add(product)
            await session.commit()

            assert await apply_supplier_price(session, product, 12_000) is False
            await session.commit()
            assert await session.scalar(select(ProductPriceAlert.id)) is None
            assert product.price == 14_000
        await engine.dispose()

    asyncio.run(scenario())


def test_pending_sale_is_sent_to_started_users_and_logged() -> None:
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
                price=14_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GPT",
                supplier_markup=2_000,
                supplier_price=12_000,
                external_stock=8,
                supplier_synced_at=datetime.now(UTC),
            )
            session.add(product)
            await session.flush()
            session.add_all(
                [
                    User(telegram_id=1, full_name="Vietnamese", language="vi", has_started=True),
                    User(telegram_id=2, full_name="English", language="en", has_started=True),
                    User(telegram_id=3, full_name="Blocked", language="vi", has_started=True),
                    User(telegram_id=4, full_name="Inactive", language="vi", has_started=False),
                    ProductPriceAlert(
                        product_id=product.id,
                        provider="sumistore",
                        supplier_price_before=15_000,
                        supplier_price_after=12_000,
                        sale_price_before=17_000,
                        sale_price_after=14_000,
                    ),
                ]
            )
            await session.commit()

        bot = FakeSaleBot(failing_user_id=3)
        processed = await deliver_pending_sale_alerts(
            sessions,
            bot,  # type: ignore[arg-type]
            throttle_seconds=0,
        )
        assert processed == 1
        assert [call[0] for call in bot.calls] == [1, 2, 3]
        assert "Giá sale còn: <b>14.000đ</b>" in bot.calls[0][1]
        assert "Sale price: <b>14.000đ</b>" in bot.calls[1][1]
        assert bot.calls[0][2].inline_keyboard[0][0].callback_data == f"prod:{product.id}"

        async with sessions() as session:
            alert = await session.scalar(select(ProductPriceAlert))
            deliveries = list(
                await session.scalars(
                    select(ProductAlertDelivery).order_by(ProductAlertDelivery.user_id)
                )
            )
            blocked = await session.get(User, 3)
            assert alert is not None and alert.status == "sent"
            assert alert.total_recipients == 3
            assert alert.delivered_count == 2
            assert alert.failed_count == 1
            assert [delivery.status for delivery in deliveries] == ["sent", "sent", "failed"]
            assert blocked is not None and blocked.has_started is False
        await engine.dispose()

    asyncio.run(scenario())


def test_sale_alert_resumes_without_resending_completed_recipients() -> None:
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
                price=14_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GPT",
                supplier_price=12_000,
                external_stock=8,
                supplier_synced_at=datetime.now(UTC),
            )
            session.add(product)
            await session.flush()
            alert = ProductPriceAlert(
                product_id=product.id,
                provider="sumistore",
                supplier_price_before=15_000,
                supplier_price_after=12_000,
                sale_price_before=17_000,
                sale_price_after=14_000,
                status="sending",
                total_recipients=3,
                started_at=datetime.now(UTC),
            )
            session.add(alert)
            session.add_all(
                [
                    User(telegram_id=1, full_name="Done", has_started=True),
                    User(telegram_id=2, full_name="Interrupted", has_started=True),
                    User(telegram_id=3, full_name="Pending", has_started=True),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    ProductAlertDelivery(
                        alert_type="sale",
                        alert_id=alert.id,
                        user_id=1,
                        language="vi",
                        status="sent",
                    ),
                    ProductAlertDelivery(
                        alert_type="sale",
                        alert_id=alert.id,
                        user_id=2,
                        language="vi",
                        status="sending",
                    ),
                    ProductAlertDelivery(
                        alert_type="sale",
                        alert_id=alert.id,
                        user_id=3,
                        language="vi",
                        status="pending",
                    ),
                ]
            )
            await session.commit()

        await recover_interrupted_product_alerts(sessions)
        bot = FakeSaleBot()
        processed = await deliver_pending_sale_alerts(
            sessions,
            bot,  # type: ignore[arg-type]
            throttle_seconds=0,
        )

        assert processed == 1
        assert sorted(call[0] for call in bot.calls) == [2, 3]
        async with sessions() as session:
            alert = await session.scalar(select(ProductPriceAlert))
            assert alert is not None and alert.status == "sent"
            assert alert.delivered_count == 3
            assert alert.failed_count == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_sale_alert_waits_until_stock_is_available() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Hết hàng",
                name_en="Out of stock",
                price=12_000,
                fulfillment_source="lehai",
                supplier_price=10_000,
                supplier_markup=2_000,
                external_stock=0,
                supplier_synced_at=datetime.now(UTC),
            )
            session.add(product)
            await session.flush()
            session.add(
                ProductPriceAlert(
                    product_id=product.id,
                    provider="lehai",
                    supplier_price_before=15_000,
                    supplier_price_after=10_000,
                    sale_price_before=17_000,
                    sale_price_after=12_000,
                )
            )
            await session.commit()

        bot = FakeSaleBot()
        assert (
            await deliver_pending_sale_alerts(
                sessions,
                bot,  # type: ignore[arg-type]
                throttle_seconds=0,
            )
            == 0
        )
        assert bot.calls == []
        async with sessions() as session:
            alert = await session.scalar(select(ProductPriceAlert))
            assert alert is not None and alert.status == "pending"
        await engine.dispose()

    asyncio.run(scenario())
