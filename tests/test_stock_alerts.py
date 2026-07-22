import asyncio
from datetime import UTC, datetime, timedelta

from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.broadcasts import deliver_pending_stock_alerts
from app.database import Base
from app.models import (
    Category,
    FlashSaleCampaign,
    InventoryItem,
    Product,
    ProductAlertDelivery,
    ProductStockAlert,
    User,
)
from app.stock_alerts import apply_supplier_stock, stock_alert_enabled


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
                supplier_product_id="SP-GEF55PBV",
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
        assert "FLASH SALE" not in bot.calls[0][1]
        assert "Kho vừa có: <b>6</b>" in bot.calls[0][1]
        assert "PRODUCT BACK IN STOCK" in bot.calls[1][1]
        assert bot.calls[0][2].inline_keyboard[0][0].callback_data == f"prod:{product.id}"

        async with sessions() as session:
            alerts = list(
                await session.scalars(select(ProductStockAlert).order_by(ProductStockAlert.id))
            )
            deliveries = list(
                await session.scalars(
                    select(ProductAlertDelivery).order_by(ProductAlertDelivery.user_id)
                )
            )
            blocked = await session.get(User, 3)
            assert len(alerts) == 1
            assert alerts[0].status == "sent"
            assert alerts[0].stock_before == 4
            assert alerts[0].stock_after == 6
            assert alerts[0].total_recipients == 3
            assert alerts[0].delivered_count == 2
            assert alerts[0].failed_count == 1
            assert alerts[0].message_vi == bot.calls[0][1]
            assert alerts[0].message_en == bot.calls[1][1]
            assert [delivery.status for delivery in deliveries] == ["sent", "sent", "failed"]
            assert blocked is not None and blocked.has_started is False
        await engine.dispose()

    asyncio.run(scenario())


def test_stock_return_during_flash_sale_uses_campaign_price_and_remaining_quantity() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus Flash",
                name_en="GPT Plus Flash",
                price=20_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_price=10_000,
                external_stock=0,
                supplier_available_stock=0,
                supplier_available_stock_initialized=True,
            )
            session.add(product)
            await session.flush()
            session.add(
                FlashSaleCampaign(
                    product_id=product.id,
                    original_price=20_000,
                    sale_price=15_000,
                    total_quantity=20,
                    sold_quantity=4,
                    reserved_quantity=3,
                    message_text="Flash campaign",
                    notification_status="sent",
                )
            )
            session.add(
                User(telegram_id=1, full_name="Buyer", language="vi", has_started=True)
            )
            await session.commit()

            assert await apply_supplier_stock(session, product, 9) is True
            product.external_stock = 9
            await session.commit()

        bot = FakeStockBot()
        assert await deliver_pending_stock_alerts(
            sessions,
            bot,  # type: ignore[arg-type]
            throttle_seconds=0,
        ) == 1
        assert len(bot.calls) == 1
        message = bot.calls[0][1]
        keyboard = bot.calls[0][2]
        assert "HÀNG MỚI VỀ · FLASH SALE" in message
        assert "Giá Flash Sale: <b>15.000đ</b>" in message
        assert "Còn lại: <b>13 đơn Flash Sale</b>" in message
        assert "20.000đ" not in message
        assert "15.000đ" in keyboard.inline_keyboard[0][0].text
        assert keyboard.inline_keyboard[0][0].callback_data == f"prod:{product.id}"

        async with sessions() as session:
            alert = await session.scalar(select(ProductStockAlert))
            assert alert is not None
            assert alert.status == "sent"
            assert alert.sale_price == 15_000
            assert alert.message_vi == message
            assert "13 đơn Flash Sale" in str(alert.message_vi)
        await engine.dispose()

    asyncio.run(scenario())


def test_always_stock_alert_waits_ten_minutes_and_sends_latest_increase() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Kho thay đổi nhanh",
                name_en="Fast-changing stock",
                price=20_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                notify_stock_without_balance_topup=True,
                supplier_available_stock=0,
                supplier_available_stock_initialized=True,
                external_stock=0,
            )
            session.add(product)
            session.add(User(telegram_id=1, full_name="Buyer", language="vi", has_started=True))
            await session.commit()

            assert await apply_supplier_stock(session, product, 5) is True
            product.external_stock = 5
            await session.commit()

        bot = FakeStockBot()
        assert await deliver_pending_stock_alerts(
            sessions,
            bot,  # type: ignore[arg-type]
            throttle_seconds=0,
        ) == 1
        assert len(bot.calls) == 1
        assert "<b>5</b>" in bot.calls[0][1]

        async with sessions() as session:
            product = await session.scalar(select(Product))
            assert product is not None
            assert await apply_supplier_stock(session, product, 6) is True
            product.external_stock = 6
            await session.commit()
            assert await apply_supplier_stock(session, product, 8) is False
            product.external_stock = 8
            await session.commit()

        # A newer increase is queued, but cannot start another broadcast yet.
        assert await deliver_pending_stock_alerts(
            sessions,
            bot,  # type: ignore[arg-type]
            throttle_seconds=0,
        ) == 0
        assert len(bot.calls) == 1

        async with sessions() as session:
            alerts = list(
                await session.scalars(select(ProductStockAlert).order_by(ProductStockAlert.id))
            )
            assert len(alerts) == 2
            assert alerts[1].status == "pending"
            assert alerts[1].stock_before == 6
            assert alerts[1].stock_after == 8
            alerts[0].sent_at = datetime.now(UTC) - timedelta(minutes=11)
            alerts[0].completed_at = alerts[0].sent_at
            await session.commit()

        assert await deliver_pending_stock_alerts(
            sessions,
            bot,  # type: ignore[arg-type]
            throttle_seconds=0,
        ) == 1
        assert len(bot.calls) == 2
        assert "<b>8</b>" in bot.calls[1][1]
        await engine.dispose()

    asyncio.run(scenario())


def test_always_stock_alert_drops_queued_notice_when_latest_change_is_a_decrease() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Kho thay đổi nhanh",
                name_en="Fast-changing stock",
                price=20_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                notify_stock_without_balance_topup=True,
                supplier_available_stock=0,
                supplier_available_stock_initialized=True,
                external_stock=0,
            )
            session.add(product)
            session.add(User(telegram_id=1, full_name="Buyer", language="vi", has_started=True))
            await session.commit()
            assert await apply_supplier_stock(session, product, 5) is True
            product.external_stock = 5
            await session.commit()

        bot = FakeStockBot()
        assert await deliver_pending_stock_alerts(
            sessions,
            bot,  # type: ignore[arg-type]
            throttle_seconds=0,
        ) == 1

        async with sessions() as session:
            product = await session.scalar(select(Product))
            assert product is not None
            assert await apply_supplier_stock(session, product, 8) is True
            product.external_stock = 8
            await session.commit()
            assert await apply_supplier_stock(session, product, 6) is False
            product.external_stock = 6
            first_alert = await session.scalar(
                select(ProductStockAlert).order_by(ProductStockAlert.id).limit(1)
            )
            assert first_alert is not None
            first_alert.sent_at = datetime.now(UTC) - timedelta(minutes=11)
            first_alert.completed_at = first_alert.sent_at
            await session.commit()

        assert await deliver_pending_stock_alerts(
            sessions,
            bot,  # type: ignore[arg-type]
            throttle_seconds=0,
        ) == 0
        assert len(bot.calls) == 1
        async with sessions() as session:
            alerts = list(
                await session.scalars(select(ProductStockAlert).order_by(ProductStockAlert.id))
            )
            assert [alert.status for alert in alerts] == ["sent", "superseded"]
            assert alerts[1].stock_before == 8
            assert alerts[1].stock_after == 6
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
                supplier_product_id="SP-GEF55PBV",
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


def test_stock_increase_while_available_is_queued() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Stock increase",
                name_en="Stock increase",
                price=20_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_available_stock=8,
                supplier_available_stock_initialized=True,
                external_stock=8,
            )
            session.add(product)
            await session.commit()

            assert await apply_supplier_stock(session, product, 12) is True
            product.external_stock = 12
            await session.commit()
            alert = await session.scalar(select(ProductStockAlert))
            assert alert is not None
            assert alert.stock_before == 8
            assert alert.stock_after == 12
        await engine.dispose()

    asyncio.run(scenario())


def test_non_alert_product_updates_stock_without_queueing_notification() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT trắng",
                name_en="ChatGPT white",
                price=2_500,
                fulfillment_source="sumistore",
                supplier_product_id="SP-JMYJL2PL",
                external_stock=0,
            )
            session.add(product)
            await session.flush()

            assert await apply_supplier_stock(session, product, 100) is False
            product.external_stock = 100
            await session.commit()

            assert product.supplier_available_stock == 100
            assert await session.scalar(select(ProductStockAlert.id)) is None
        await engine.dispose()

    asyncio.run(scenario())


def test_manual_stock_zero_updates_source_stock_without_announcing() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus tạm dừng",
                name_en="Paused GPT Plus",
                price=20_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                force_out_of_stock=True,
                supplier_available_stock=5,
                supplier_available_stock_initialized=True,
            )
            session.add(product)
            await session.flush()

            assert await apply_supplier_stock(session, product, 20) is False
            await session.commit()

            assert product.supplier_available_stock == 20
            assert await session.scalar(select(ProductStockAlert.id)) is None
        await engine.dispose()

    asyncio.run(scenario())


def test_price_lock_releases_only_after_imported_inventory_is_empty() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Hàng ôm",
                name_en="Stocked item",
                price=28_000,
                fulfillment_source="lehai",
                supplier_product_id="cdk_ggpro_18m",
                supplier_price=27_000,
                supplier_markup=8_000,
                price_lock_enabled=True,
                external_stock=1,
            )
            session.add(product)
            await session.flush()
            item = InventoryItem(
                product_id=product.id,
                encrypted_secret="encrypted",
                cost_amount=20_000,
            )
            session.add(item)
            await session.commit()

            await apply_supplier_stock(
                session,
                product,
                10,
                local_inventory_stock=0,
            )
            await session.commit()
            assert product.price_lock_enabled is True
            assert product.price == 28_000

            item.status = "sold"
            await session.flush()
            await apply_supplier_stock(
                session,
                product,
                10,
                local_inventory_stock=0,
            )
            await session.commit()
            assert product.price_lock_enabled is False
            assert product.price == 35_000
        await engine.dispose()

    asyncio.run(scenario())


def test_only_jio_is_featured_for_lehai_stock_notifications() -> None:
    pixel = Product(
        category_id=1,
        name_vi="Pixel",
        name_en="Pixel",
        price=30_000,
        fulfillment_source="lehai",
        supplier_product_id="cdk_pixel",
    )
    jio = Product(
        category_id=1,
        name_vi="Jio 18M",
        name_en="Jio 18M",
        price=35_000,
        fulfillment_source="lehai",
        supplier_product_id="cdk_ggpro_18m",
    )
    bhf = Product(
        category_id=1,
        name_vi="BHF GPT Plus",
        name_en="BHF GPT Plus",
        price=135_000,
        fulfillment_source="lehai",
        supplier_product_id="gptupi_kbh12k",
    )

    assert stock_alert_enabled(pixel) is False
    assert stock_alert_enabled(jio) is True
    assert stock_alert_enabled(bhf) is False
    bhf.notify_stock_without_balance_topup = True
    assert stock_alert_enabled(bhf) is True
