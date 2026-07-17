import asyncio
import contextlib
import logging

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand, BotCommandScopeChat
from sqlalchemy import select, text

from app.admin import create_admin_router
from app.api import create_api
from app.config import get_settings
from app.database import Base, DatabaseSessionMiddleware, create_database
from app.handlers import create_router
from app.models import Category, Product
from app.supplier_audit import reconcile_supplier_balance
from app.suppliers import (
    SumistoreClient,
    create_sumistore_client,
    ensure_sumistore_product,
    sync_sumistore_products,
)
from app.utils import SecretCipher, format_vnd


async def initialize_database(engine, session_factory, seed_demo_data: bool) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "ALTER TABLE deposits ADD COLUMN IF NOT EXISTS "
                "payment_kind VARCHAR(20) NOT NULL DEFAULT 'wallet'"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE deposits ADD COLUMN IF NOT EXISTS "
                "product_id INTEGER NULL REFERENCES products(id)"
            )
        )
        await connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_deposits_product_id ON deposits (product_id)")
        )
        await connection.execute(
            text(
                "ALTER TABLE deposits ADD COLUMN IF NOT EXISTS quantity INTEGER NOT NULL DEFAULT 1"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS "
                "product_type VARCHAR(20) NOT NULL DEFAULT 'account'"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS "
                "allow_quantity BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS "
                "max_quantity INTEGER NOT NULL DEFAULT 10"
            )
        )
        await connection.execute(
            text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS batch_code VARCHAR(32) NULL")
        )
        await connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_orders_batch_code ON orders (batch_code)")
        )
        await connection.execute(
            text(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS "
                "fulfillment_source VARCHAR(20) NOT NULL DEFAULT 'local'"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS supplier_product_id VARCHAR(64) NULL"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS "
                "supplier_markup BIGINT NOT NULL DEFAULT 0"
            )
        )
        await connection.execute(
            text("ALTER TABLE products ADD COLUMN IF NOT EXISTS supplier_price BIGINT NULL")
        )
        await connection.execute(
            text(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS "
                "external_stock INTEGER NOT NULL DEFAULT 0"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS supplier_synced_at TIMESTAMPTZ NULL"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS "
                "cost_amount BIGINT NOT NULL DEFAULT 0"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE api_clients ADD COLUMN IF NOT EXISTS "
                "admin_blocked BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_api_clients_admin_blocked "
                "ON api_clients (admin_blocked)"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS "
                "discount_amount BIGINT NOT NULL DEFAULT 0"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS "
                "discount_code_id INTEGER NULL REFERENCES discount_codes(id) ON DELETE SET NULL"
            )
        )
        await connection.execute(
            text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS discount_code VARCHAR(64) NULL")
        )
        await connection.execute(
            text(
                "ALTER TABLE deposits ADD COLUMN IF NOT EXISTS "
                "discount_amount BIGINT NOT NULL DEFAULT 0"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE deposits ADD COLUMN IF NOT EXISTS "
                "discount_code_id INTEGER NULL REFERENCES discount_codes(id) ON DELETE SET NULL"
            )
        )
        await connection.execute(
            text("ALTER TABLE deposits ADD COLUMN IF NOT EXISTS discount_code VARCHAR(64) NULL")
        )
        await connection.execute(
            text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS supplier_order_code VARCHAR(64) NULL")
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_products_fulfillment_source "
                "ON products (fulfillment_source)"
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_products_supplier_product_id "
                "ON products (supplier_product_id)"
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_orders_supplier_order_code "
                "ON orders (supplier_order_code)"
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_orders_discount_code_id "
                "ON orders (discount_code_id)"
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_deposits_discount_code_id "
                "ON deposits (discount_code_id)"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "has_started BOOLEAN NOT NULL DEFAULT TRUE"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE categories ADD COLUMN IF NOT EXISTS "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_users_has_started "
                "ON users (has_started)"
            )
        )
        await connection.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_code VARCHAR(24) NULL")
        )
        await connection.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "referred_by_id BIGINT NULL REFERENCES users(telegram_id) ON DELETE SET NULL"
            )
        )
        await connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_referral_code "
                "ON users (referral_code) WHERE referral_code IS NOT NULL"
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_users_referred_by_id "
                "ON users (referred_by_id)"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS "
                "sales_channel VARCHAR(16) NOT NULL DEFAULT 'telegram'"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS "
                "api_client_id INTEGER NULL REFERENCES api_clients(id) ON DELETE SET NULL"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS "
                "api_order_request_id INTEGER NULL REFERENCES api_order_requests(id) "
                "ON DELETE SET NULL"
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_orders_sales_channel "
                "ON orders (sales_channel)"
            )
        )
        await connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_orders_api_client_id ON orders (api_client_id)")
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_orders_api_order_request_id "
                "ON orders (api_order_request_id)"
            )
        )
        await connection.execute(
            text("UPDATE products SET allow_quantity = TRUE WHERE name_en = 'Demo account'")
        )
        await connection.execute(
            text(
                "UPDATE orders SET cost_amount = COALESCE(products.supplier_price, 0) "
                "FROM products WHERE orders.product_id = products.id "
                "AND products.fulfillment_source = 'sumistore' AND orders.cost_amount = 0"
            )
        )

    if not seed_demo_data:
        return
    async with session_factory() as session:
        if await session.scalar(select(Category.id).limit(1)) is not None:
            return
        accounts = Category(name_vi="Tài khoản", name_en="Accounts", position=1)
        session.add(accounts)
        await session.flush()
        session.add(
            Product(
                category_id=accounts.id,
                name_vi="Tài khoản mẫu",
                name_en="Demo account",
                description_vi="Sản phẩm mẫu, quản trị viên có thể sửa hoặc thay thế.",
                description_en="Demo product. Replace it before opening the shop.",
                price=50_000,
                allow_quantity=True,
            )
        )
        await session.commit()


async def supplier_sync_worker(
    session_factory,
    client: SumistoreClient,
    interval_seconds: int,
) -> None:
    while True:
        try:
            await sync_sumistore_products(session_factory, client)
        except Exception:
            logging.getLogger(__name__).exception("Could not synchronize supplier products")
        await asyncio.sleep(max(15, interval_seconds))


async def supplier_audit_worker(
    session_factory,
    client: SumistoreClient,
    bot: Bot,
    admin_ids: tuple[int, ...],
    interval_seconds: int,
) -> None:
    while True:
        try:
            result = await reconcile_supplier_balance(session_factory, client)
            if result.suspicious_amount < 0:
                message = (
                    "🚨 <b>Phát hiện giao dịch Sumi đáng ngờ</b>\n"
                    f"Số tiền không khớp: <b>-{format_vnd(abs(result.suspicious_amount))}</b>\n"
                    f"Số dư hiện tại: <b>{format_vnd(result.current_balance)}</b>\n\n"
                    "Mở Admin → Giao dịch đáng ngờ để xem kỳ đối soát."
                )
                for admin_id in admin_ids:
                    try:
                        await bot.send_message(admin_id, message)
                    except Exception:
                        logging.getLogger(__name__).exception(
                            "Could not notify admin %s about supplier balance anomaly",
                            admin_id,
                        )
        except Exception:
            logging.getLogger(__name__).exception("Could not reconcile supplier balance")
        await asyncio.sleep(max(10, interval_seconds))


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Supplier polling is frequent; keep successful HTTP requests out of production logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    engine, session_factory = create_database(settings.database_url)
    await initialize_database(engine, session_factory, settings.seed_demo_data)
    await ensure_sumistore_product(session_factory, settings)
    supplier_client = create_sumistore_client(settings)
    await sync_sumistore_products(session_factory, supplier_client)
    if supplier_client is not None:
        try:
            await reconcile_supplier_balance(session_factory, supplier_client)
        except Exception:
            logging.getLogger(__name__).exception("Could not initialize supplier balance audit")

    cipher = SecretCipher(settings.inventory_encryption_key.get_secret_value())
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    notification_token = settings.deposit_notification_bot_token.get_secret_value()
    deposit_notification_bot = (
        Bot(
            token=notification_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        if notification_token
        else None
    )
    customer_commands = [
        BotCommand(command="start", description="Mở menu chính"),
        BotCommand(command="muanhanh", description="Mua nhanh sản phẩm"),
        BotCommand(command="naptien", description="Nạp tiền tự động"),
        BotCommand(command="donmua", description="Xem đơn đã mua"),
        BotCommand(command="hoso", description="Xem hồ sơ và số dư"),
        BotCommand(command="donchat", description="Dọn chat và mở menu mới"),
        BotCommand(command="hotro", description="Liên hệ hỗ trợ"),
    ]
    await bot.set_my_commands(customer_commands)
    admin_commands = [
        *customer_commands,
        BotCommand(command="admin", description="Mở bảng quản trị Telegram"),
        BotCommand(command="products", description="Xem sản phẩm và tồn kho"),
        BotCommand(command="thongbao", description="Gửi thông báo tới khách hàng"),
    ]
    for admin_id in settings.admin_ids:
        await bot.set_my_commands(
            admin_commands,
            scope=BotCommandScopeChat(chat_id=admin_id),
        )
    storage = RedisStorage.from_url(settings.redis_url)
    dispatcher = Dispatcher(storage=storage)
    dispatcher.update.outer_middleware(DatabaseSessionMiddleware(session_factory))
    dispatcher.include_router(create_admin_router(settings, cipher))
    dispatcher.include_router(create_router(settings, cipher, supplier_client))

    api = create_api(
        settings,
        session_factory,
        bot,
        cipher,
        supplier_client,
        deposit_notification_bot,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            api,
            host=settings.web_host,
            port=settings.web_port,
            log_level=settings.log_level.lower(),
        )
    )
    api_task = asyncio.create_task(server.serve())
    supplier_task = (
        asyncio.create_task(
            supplier_sync_worker(
                session_factory,
                supplier_client,
                settings.sumistore_sync_seconds,
            )
        )
        if supplier_client is not None
        else None
    )
    supplier_audit_task = (
        asyncio.create_task(
            supplier_audit_worker(
                session_factory,
                supplier_client,
                bot,
                settings.admin_ids,
                settings.sumistore_audit_seconds,
            )
        )
        if supplier_client is not None
        else None
    )
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(bot)
    finally:
        if supplier_task is not None:
            supplier_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await supplier_task
        if supplier_audit_task is not None:
            supplier_audit_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await supplier_audit_task
        server.should_exit = True
        await api_task
        await storage.close()
        if deposit_notification_bot is not None:
            await deposit_notification_bot.session.close()
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
