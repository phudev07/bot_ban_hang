import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from html import escape

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand, BotCommandScopeChat
from sqlalchemy import delete, select, text

from app.admin import create_admin_router
from app.api import create_api
from app.broadcasts import (
    BroadcastRateLimiter,
    backfill_stock_alert_messages,
    broadcast_worker,
    sale_alert_worker,
)
from app.config import get_settings
from app.database import Base, DatabaseSessionMiddleware, create_database
from app.handlers import create_router
from app.keyboards import sms_waiting_menu
from app.lehai_suppliers import (
    LeHaiPremiumClient,
    create_lehai_client,
    ensure_lehai_products,
    sync_lehai_products,
)
from app.models import ApiRequestAudit, Category, Product
from app.payment_expiry import payment_expiry_worker
from app.rate_limit import BotSpamProtectionMiddleware
from app.rentsim import RentSimClient, create_rentsim_client
from app.sms_rentals import (
    mark_sms_review_alerted,
    pending_sms_review_alerts,
    poll_pending_sms_rentals,
)
from app.supplier_audit import (
    mark_supplier_alerted,
    pending_unresolved_supplier_alerts,
    reconcile_historical_supplier_refunds,
    reconcile_supplier_balance,
)
from app.supplier_recovery import recover_pending_sumistore_orders
from app.suppliers import (
    ExternalSupplierClient,
    SumistoreClient,
    SupplierError,
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
                "ALTER TABLE broadcast_logs ADD COLUMN IF NOT EXISTS "
                "status VARCHAR(20) NOT NULL DEFAULT 'completed'"
            )
        )
        await connection.execute(
            text("ALTER TABLE broadcast_logs ALTER COLUMN status SET DEFAULT 'queued'")
        )
        await connection.execute(
            text(
                "ALTER TABLE broadcast_logs ADD COLUMN IF NOT EXISTS "
                "started_at TIMESTAMPTZ NULL"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE broadcast_logs ADD COLUMN IF NOT EXISTS "
                "completed_at TIMESTAMPTZ NULL"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE broadcast_logs ADD COLUMN IF NOT EXISTS "
                "last_error VARCHAR(500) NULL"
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_broadcast_logs_status "
                "ON broadcast_logs (status)"
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_broadcast_deliveries_campaign_status "
                "ON broadcast_deliveries (broadcast_id, status, id)"
            )
        )
        for table_name in ("product_price_alerts", "product_stock_alerts"):
            await connection.execute(
                text(
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS "
                    "started_at TIMESTAMPTZ NULL"
                )
            )
            await connection.execute(
                text(
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS "
                    "completed_at TIMESTAMPTZ NULL"
                )
            )
            await connection.execute(
                text(
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS "
                    "last_error VARCHAR(500) NULL"
                )
            )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_product_alert_deliveries_alert_status "
                "ON product_alert_deliveries (alert_type, alert_id, status, id)"
            )
        )
        await connection.execute(
            text(
                "DO $$ BEGIN "
                "IF NOT EXISTS ("
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'supplier_balance_transactions' "
                "AND column_name = 'admin_alerted_at'"
                ") THEN "
                "ALTER TABLE supplier_balance_transactions "
                "ADD COLUMN admin_alerted_at TIMESTAMPTZ NULL; "
                "UPDATE supplier_balance_transactions SET admin_alerted_at = created_at "
                "WHERE kind = 'suspicious'; "
                "END IF; END $$"
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_supplier_balance_pending_admin_alert "
                "ON supplier_balance_transactions "
                "(provider, kind, admin_alerted_at, created_at, id)"
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_flash_sale_campaigns_product_status "
                "ON flash_sale_campaigns (product_id, status, id)"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS "
                "flash_sale_id INTEGER NULL REFERENCES flash_sale_campaigns(id) "
                "ON DELETE SET NULL"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE deposits ADD COLUMN IF NOT EXISTS "
                "flash_sale_id INTEGER NULL REFERENCES flash_sale_campaigns(id) "
                "ON DELETE SET NULL"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE deposits ADD COLUMN IF NOT EXISTS "
                "flash_sale_quantity INTEGER NOT NULL DEFAULT 0"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE deposits ADD COLUMN IF NOT EXISTS "
                "inventory_price_locked BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_orders_flash_sale_id "
                "ON orders (flash_sale_id)"
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_deposits_flash_sale_id "
                "ON deposits (flash_sale_id)"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE sms_rentals ADD COLUMN IF NOT EXISTS "
                "rental_message_id BIGINT NULL"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE sms_rentals ADD COLUMN IF NOT EXISTS "
                "provider_balance_after BIGINT NULL"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE sms_rentals ADD COLUMN IF NOT EXISTS "
                "review_alerted_at TIMESTAMPTZ NULL"
            )
        )
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
                "sumistore_api_enabled BOOLEAN NOT NULL DEFAULT TRUE"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS "
                "lehai_api_enabled BOOLEAN NOT NULL DEFAULT TRUE"
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
                "price_lock_enabled BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS "
                "external_stock INTEGER NOT NULL DEFAULT 0"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS "
                "force_out_of_stock BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS "
                "notify_stock_without_balance_topup BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS "
                "supplier_available_stock INTEGER NOT NULL DEFAULT 0"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS "
                "supplier_available_stock_initialized BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS "
                "supplier_owner_balance BIGINT NULL"
            )
        )
        await connection.execute(
            text(
                "UPDATE products SET supplier_owner_balance = supplier_balance_states.last_balance "
                "FROM supplier_balance_states "
                "WHERE products.fulfillment_source = supplier_balance_states.provider "
                "AND products.supplier_owner_balance IS NULL"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE product_stock_alerts ADD COLUMN IF NOT EXISTS "
                "message_vi TEXT NULL"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE product_stock_alerts ADD COLUMN IF NOT EXISTS "
                "message_en TEXT NULL"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS supplier_synced_at TIMESTAMPTZ NULL"
            )
        )
        await connection.execute(
            text(
                "UPDATE products SET "
                "supplier_available_stock = GREATEST(external_stock, 0), "
                "supplier_available_stock_initialized = TRUE "
                "WHERE supplier_available_stock_initialized = FALSE "
                "AND supplier_synced_at IS NOT NULL "
                "AND fulfillment_source IN ('sumistore', 'lehai')"
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
            text("ALTER TABLE deposits ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ NULL")
        )
        await connection.execute(
            text("ALTER TABLE deposits ADD COLUMN IF NOT EXISTS failed_at TIMESTAMPTZ NULL")
        )
        await connection.execute(
            text("ALTER TABLE deposits ADD COLUMN IF NOT EXISTS failure_reason VARCHAR(64) NULL")
        )
        await connection.execute(
            text("ALTER TABLE deposits ADD COLUMN IF NOT EXISTS telegram_chat_id BIGINT NULL")
        )
        await connection.execute(
            text(
                "ALTER TABLE deposits ADD COLUMN IF NOT EXISTS "
                "telegram_message_ids TEXT NOT NULL DEFAULT ''"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE deposits ADD COLUMN IF NOT EXISTS "
                "messages_deleted_at TIMESTAMPTZ NULL"
            )
        )
        await connection.execute(
            text(
                "UPDATE deposits SET expires_at = created_at + INTERVAL '5 minutes' "
                "WHERE expires_at IS NULL"
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_deposits_status_expires_at "
                "ON deposits (status, expires_at)"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE payment_transactions ADD COLUMN IF NOT EXISTS "
                "credit_status VARCHAR(32) NOT NULL DEFAULT 'credited'"
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_payment_transactions_credit_status "
                "ON payment_transactions (credit_status)"
            )
        )
        await connection.execute(
            text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS supplier_order_code VARCHAR(64) NULL")
        )
        await connection.execute(
            text(
                "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS "
                "cost_amount BIGINT NOT NULL DEFAULT 0"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS "
                "supplier_order_code VARCHAR(64) NULL"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS "
                "supplier_item_index INTEGER NULL"
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_inventory_items_supplier_order_code "
                "ON inventory_items (supplier_order_code)"
            )
        )
        await connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_inventory_supplier_source "
                "ON inventory_items (supplier_order_code, supplier_item_index)"
            )
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
                "CREATE INDEX IF NOT EXISTS ix_sms_rentals_status_last_checked "
                "ON sms_rentals (status, last_checked_at, id)"
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_sms_rentals_status_requested "
                "ON sms_rentals (status, requested_at, id)"
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_sms_rentals_user_requested "
                "ON sms_rentals (user_id, requested_at, id)"
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
            text(
                "CREATE INDEX IF NOT EXISTS ix_orders_api_client_request "
                "ON orders (api_client_id, api_order_request_id, id)"
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_api_order_requests_client_status_id "
                "ON api_order_requests (api_client_id, status, id)"
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_api_order_requests_client_order_code "
                "ON api_order_requests (api_client_id, shop_order_code)"
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_api_request_audits_client_created "
                "ON api_request_audits (api_client_id, created_at)"
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
        await connection.execute(
            text(
                "INSERT INTO wallet_transactions "
                "(user_id, kind, amount, balance_before, balance_after, reference_type, "
                "reference_id, event_key, description, created_at) "
                "SELECT users.telegram_id, 'opening_balance', users.balance, 0, users.balance, "
                "'system', CAST(users.telegram_id AS VARCHAR), "
                "'opening:' || CAST(users.telegram_id AS VARCHAR), "
                "'Số dư đầu kỳ khi bật sổ phát sinh', NOW() "
                "FROM users "
                "ON CONFLICT (event_key) DO NOTHING"
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
    lehai_client: LeHaiPremiumClient | None = None,
) -> None:
    while True:
        try:
            await sync_sumistore_products(
                session_factory,
                client,
                lehai_client,
            )
        except Exception:
            logging.getLogger(__name__).exception("Could not synchronize supplier products")
        await asyncio.sleep(max(15, interval_seconds))


async def supplier_recovery_worker(
    session_factory,
    client: SumistoreClient,
    cipher: SecretCipher,
    interval_seconds: int = 15,
) -> None:
    while True:
        try:
            result = await recover_pending_sumistore_orders(
                session_factory,
                client,
                cipher,
            )
            if result.matched_orders or result.linked_audits or result.queued_orphans:
                logging.getLogger(__name__).warning(
                    "Recovered delayed Sumi orders: orders=%s accounts=%s audits=%s "
                    "orphan_requests=%s",
                    result.matched_orders,
                    result.inserted_accounts,
                    result.linked_audits,
                    result.queued_orphans,
                )
        except Exception:
            logging.getLogger(__name__).exception(
                "Could not recover delayed Sumi orders"
            )
        await asyncio.sleep(max(10, interval_seconds))


async def lehai_sync_worker(
    session_factory,
    client: LeHaiPremiumClient,
    interval_seconds: int,
    sumistore_client: SumistoreClient | None = None,
) -> None:
    while True:
        try:
            await sync_lehai_products(
                session_factory,
                client,
                sumistore_client,
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "Could not synchronize Le Hai Premium products"
            )
        await asyncio.sleep(max(15, interval_seconds))


async def notify_unresolved_supplier_alerts(
    session_factory,
    bot: Bot,
    admin_ids: tuple[int, ...],
    *,
    provider: str,
    provider_label: str,
) -> int:
    alerts = await pending_unresolved_supplier_alerts(
        session_factory,
        provider=provider,
    )
    sent_count = 0
    for alert in alerts:
        before = (
            format_vnd(alert.balance_before)
            if alert.balance_before is not None
            else "không đọc được"
        )
        after = (
            format_vnd(alert.balance_after)
            if alert.balance_after is not None
            else "không đọc được"
        )
        message = (
            f"🚨 <b>Không thể tự thu hồi giao dịch {escape(provider_label)}</b>\n\n"
            f"• Log: <code>#{alert.transaction_id}</code>\n"
            f"• Số tiền chưa khớp: <b>-{format_vnd(abs(alert.amount))}</b>\n"
            f"• Số dư nguồn: <b>{before} → {after}</b>\n\n"
            "Đã hết thời gian tự thu hồi/hoàn tiền nhưng giao dịch vẫn chưa "
            "được xử lý. Mở Admin → Giao dịch đáng ngờ để kiểm tra."
        )
        delivered = False
        for admin_id in admin_ids:
            try:
                await bot.send_message(admin_id, message)
                delivered = True
            except Exception:
                logging.getLogger(__name__).exception(
                    "Could not notify admin %s about supplier balance anomaly",
                    admin_id,
                )
        if delivered and await mark_supplier_alerted(
            session_factory,
            alert.transaction_id,
        ):
            sent_count += 1
    return sent_count


async def supplier_audit_worker(
    session_factory,
    client: ExternalSupplierClient,
    bot: Bot,
    admin_ids: tuple[int, ...],
    interval_seconds: int,
    *,
    provider: str = "sumistore",
    provider_label: str = "Sumi",
) -> None:
    while True:
        try:
            await reconcile_supplier_balance(
                session_factory,
                client,
                provider=provider,
                provider_label=provider_label,
            )
            await notify_unresolved_supplier_alerts(
                session_factory,
                bot,
                admin_ids,
                provider=provider,
                provider_label=provider_label,
            )
        except SupplierError as exc:
            logging.getLogger(__name__).warning(
                "Could not reconcile %s balance: code=%s",
                provider_label,
                exc.code,
            )
        except Exception:
            logging.getLogger(__name__).exception("Could not reconcile supplier balance")
        await asyncio.sleep(max(10, interval_seconds))


async def rentsim_otp_worker(
    session_factory,
    client: RentSimClient,
    bot: Bot,
    admin_ids: tuple[int, ...],
    poll_seconds: int,
    referral_commission_percent: int,
    request_recovery_seconds: int,
    pending_alert_seconds: int,
) -> None:
    while True:
        try:
            notifications = await poll_pending_sms_rentals(
                session_factory,
                client,
                poll_seconds=poll_seconds,
                referral_commission_percent=referral_commission_percent,
                request_recovery_seconds=request_recovery_seconds,
            )
            for item in notifications:
                if item.status == "success":
                    text = (
                        "✅ <b>OTP received</b>\n\n"
                        f"• Order: <code>{escape(item.shop_order_code)}</code>\n"
                        f"• Number: <code>{escape(item.phone_number)}</code>\n"
                        f"• OTP: <code>{escape(item.otp_code or '—')}</code>\n"
                        f"• Message: {escape(item.otp_content or '—')}\n\n"
                        "You can rent another number once 60 seconds have passed from this rental."
                        if item.language == "en"
                        else "✅ <b>Đã nhận được OTP</b>\n\n"
                        f"• Mã đơn: <code>{escape(item.shop_order_code)}</code>\n"
                        f"• Số điện thoại: <code>{escape(item.phone_number)}</code>\n"
                        f"• Mã OTP: <code>{escape(item.otp_code or '—')}</code>\n"
                        f"• Nội dung: {escape(item.otp_content or '—')}\n\n"
                        "Bạn có thể thuê số tiếp theo sau khi đủ 60 giây tính từ lượt thuê này."
                    )
                elif item.status == "refunded":
                    request_failed = item.failure_reason == "provider_request_not_confirmed"
                    if request_failed and item.language == "en":
                        text = (
                            "↩️ <b>SMS rental was refunded</b>\n\n"
                            f"• Order: <code>{escape(item.shop_order_code)}</code>\n"
                            f"• Refunded: <b>{format_vnd(item.sale_amount)}</b>\n"
                            f"• Wallet balance: <b>{format_vnd(item.balance)}</b>\n\n"
                            "RentSim did not create an order, so the full rental amount was refunded."
                        )
                    elif request_failed:
                        text = (
                            "↩️ <b>Đã hoàn tiền thuê số</b>\n\n"
                            f"• Mã đơn: <code>{escape(item.shop_order_code)}</code>\n"
                            f"• Đã hoàn ví: <b>{format_vnd(item.sale_amount)}</b>\n"
                            f"• Số dư hiện tại: <b>{format_vnd(item.balance)}</b>\n\n"
                            "RentSim không tạo đơn thuê nên toàn bộ tiền đã được hoàn lại."
                        )
                    elif item.language == "en":
                        text = (
                            "↩️ <b>No OTP received</b>\n\n"
                            f"• Order: <code>{escape(item.shop_order_code)}</code>\n"
                            f"• Rented number: <code>{escape(item.phone_number or '—')}</code>\n"
                            f"• Refunded: <b>{format_vnd(item.sale_amount)}</b>\n"
                            f"• Wallet balance: <b>{format_vnd(item.balance)}</b>\n\n"
                            "This rented number did not receive an OTP, so the rental was refunded in full."
                        )
                    else:
                        text = (
                            "↩️ <b>Không nhận được OTP</b>\n\n"
                            f"• Mã đơn: <code>{escape(item.shop_order_code)}</code>\n"
                            f"• Số thuê: <code>{escape(item.phone_number or '—')}</code>\n"
                            f"• Đã hoàn ví: <b>{format_vnd(item.sale_amount)}</b>\n"
                            f"• Số dư hiện tại: <b>{format_vnd(item.balance)}</b>\n\n"
                            f"Số <code>{escape(item.phone_number or '—')}</code> không nhận được mã OTP nên tiền thuê đã được hoàn lại đầy đủ."
                        )
                else:
                    text = (
                        "⚠️ <b>SMS rental needs review</b>\n\n"
                        f"• Order: <code>{escape(item.shop_order_code)}</code>\n"
                        f"• Temporarily held: <b>{format_vnd(item.sale_amount)}</b>\n"
                        f"• Wallet balance: <b>{format_vnd(item.balance)}</b>\n\n"
                        "The provider result could not be confirmed. The shop has not marked the "
                        "rental as successful and has not issued an unsafe automatic refund."
                        if item.language == "en"
                        else "⚠️ <b>Đơn thuê số cần đối soát</b>\n\n"
                        f"• Mã đơn: <code>{escape(item.shop_order_code)}</code>\n"
                        f"• Đang tạm giữ: <b>{format_vnd(item.sale_amount)}</b>\n"
                        f"• Số dư ví: <b>{format_vnd(item.balance)}</b>\n\n"
                        "Kết quả từ nguồn chưa xác định nên shop chưa tính là thuê thành công và "
                        "không tự động hoàn nhầm. Admin đã được cảnh báo để kiểm tra."
                    )
                markup = sms_waiting_menu(item.language, item.sale_amount)
                try:
                    if item.status == "refunded":
                        # Keep the refund visible as a separate notification instead
                        # of replacing the original waiting message.
                        message_ids = {
                            message_id
                            for message_id in (
                                item.rental_message_id,
                                item.waiting_message_id,
                            )
                            if message_id is not None
                        }
                        for message_id in message_ids:
                            try:
                                await bot.delete_message(
                                    chat_id=item.user_id,
                                    message_id=message_id,
                                )
                            except TelegramBadRequest:
                                pass
                        await bot.send_message(item.user_id, text, reply_markup=markup)
                    elif item.waiting_message_id is not None:
                        await bot.edit_message_text(
                            text,
                            chat_id=item.user_id,
                            message_id=item.waiting_message_id,
                            reply_markup=markup,
                        )
                    else:
                        await bot.send_message(item.user_id, text, reply_markup=markup)
                except TelegramBadRequest:
                    try:
                        await bot.send_message(item.user_id, text, reply_markup=markup)
                    except Exception:
                        logging.getLogger(__name__).exception(
                            "Could not send fallback RentSim result for rental %s",
                            item.rental_id,
                        )
                except Exception:
                    logging.getLogger(__name__).exception(
                        "Could not deliver RentSim OTP result for rental %s",
                        item.rental_id,
                    )
            review_alerts = await pending_sms_review_alerts(
                session_factory,
                pending_alert_seconds=pending_alert_seconds,
            )
            for review in review_alerts:
                before = (
                    format_vnd(review.provider_balance_before)
                    if review.provider_balance_before is not None
                    else "không đọc được"
                )
                after = (
                    format_vnd(review.provider_balance_after)
                    if review.provider_balance_after is not None
                    else "không đọc được"
                )
                status_label = (
                    "kết quả thuê chưa xác định"
                    if review.status == "unknown"
                    else "chờ OTP quá lâu"
                )
                alert_text = (
                    "🚨 <b>Đơn thuê SMS cần đối soát</b>\n\n"
                    f"• Mã đơn: <code>{escape(review.shop_order_code)}</code>\n"
                    f"• User: <code>{review.user_id}</code>\n"
                    f"• Trạng thái: <b>{status_label}</b>\n"
                    f"• Số thuê: <code>{escape(review.phone_number or '—')}</code>\n"
                    f"• Số dư nguồn trước/sau: <b>{before} → {after}</b>\n"
                    f"• Lỗi gần nhất: <code>{escape(review.last_error or '—')}</code>\n"
                    f"• Đã kiểm tra OTP: <b>{review.poll_attempts}</b> lần\n\n"
                    "Không hoàn thủ công nếu chưa xác minh đúng đơn tại nguồn."
                )
                delivered = False
                for admin_id in admin_ids:
                    try:
                        await bot.send_message(admin_id, alert_text)
                        delivered = True
                    except Exception:
                        logging.getLogger(__name__).exception(
                            "Could not alert admin %s about SMS rental %s",
                            admin_id,
                            review.rental_id,
                        )
                if delivered:
                    await mark_sms_review_alerted(session_factory, review.rental_id)
        except Exception:
            logging.getLogger(__name__).exception("Could not poll RentSim OTP orders")
        await asyncio.sleep(max(2, poll_seconds))


async def wait_for_server_started(
    server: uvicorn.Server,
    server_task: asyncio.Task[None],
    *,
    timeout_seconds: float = 10.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + max(1.0, timeout_seconds)
    while not server.started:
        if server_task.done():
            await server_task
            raise RuntimeError("Web server stopped before startup completed")
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("Web server did not start within the expected time")
        await asyncio.sleep(0.05)


async def startup_maintenance_worker(session_factory) -> None:
    try:
        matched_refunds = await reconcile_historical_supplier_refunds(session_factory)
        if matched_refunds:
            logging.getLogger(__name__).warning(
                "Matched historical Le Hai refunds: audits=%s",
                matched_refunds,
            )
    except Exception:
        logging.getLogger(__name__).exception("Could not reconcile historical supplier refunds")
    try:
        await backfill_stock_alert_messages(session_factory)
    except Exception:
        logging.getLogger(__name__).exception("Could not backfill stock alert messages")


async def delete_expired_api_audits(session_factory, retention_days: int) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=max(1, retention_days))
    async with session_factory() as session:
        result = await session.execute(
            delete(ApiRequestAudit).where(ApiRequestAudit.created_at < cutoff)
        )
        await session.commit()
    return max(0, int(result.rowcount or 0))


async def api_audit_cleanup_worker(
    session_factory,
    retention_days: int,
    *,
    interval_seconds: int = 6 * 60 * 60,
) -> None:
    while True:
        try:
            deleted_count = await delete_expired_api_audits(session_factory, retention_days)
            if deleted_count:
                logging.getLogger(__name__).info(
                    "Deleted expired Shop API audit rows: count=%s retention_days=%s",
                    deleted_count,
                    retention_days,
                )
        except Exception:
            logging.getLogger(__name__).exception("Could not clean expired Shop API audit rows")
        await asyncio.sleep(max(60, interval_seconds))


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Supplier polling is frequent; keep successful HTTP requests out of production logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    engine, session_factory = create_database(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
    )
    await initialize_database(engine, session_factory, settings.seed_demo_data)
    await ensure_sumistore_product(session_factory, settings)
    supplier_client = create_sumistore_client(settings)
    await ensure_lehai_products(session_factory, settings)
    lehai_client = create_lehai_client(settings)
    rentsim_client = create_rentsim_client(settings)

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
    storage = RedisStorage.from_url(settings.redis_url)
    dispatcher = Dispatcher(storage=storage)
    dispatcher.update.outer_middleware(BotSpamProtectionMiddleware(storage.redis, settings))
    dispatcher.update.outer_middleware(DatabaseSessionMiddleware(session_factory))
    dispatcher.include_router(create_admin_router(settings, cipher))
    dispatcher.include_router(
        create_router(settings, cipher, supplier_client, lehai_client, rentsim_client)
    )

    api = create_api(
        settings,
        session_factory,
        bot,
        cipher,
        supplier_client,
        deposit_notification_bot,
        api_redis=storage.redis,
        lehai_client=lehai_client,
        rentsim_client=rentsim_client,
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
    await wait_for_server_started(server, api_task)

    customer_commands = [
        BotCommand(command="start", description="Mở menu chính"),
        BotCommand(command="muanhanh", description="Mua nhanh sản phẩm"),
        BotCommand(command="naptien", description="Nạp tiền tự động"),
        BotCommand(command="donmua", description="Xem đơn đã mua"),
        BotCommand(command="hoso", description="Xem hồ sơ và số dư"),
        BotCommand(command="donchat", description="Dọn chat và mở menu mới"),
        BotCommand(command="hotro", description="Liên hệ hỗ trợ"),
    ]
    try:
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
    except Exception:
        logging.getLogger(__name__).exception("Could not configure Telegram bot commands")

    startup_maintenance_task = asyncio.create_task(
        startup_maintenance_worker(session_factory)
    )
    api_audit_cleanup_task = (
        asyncio.create_task(
            api_audit_cleanup_worker(
                session_factory,
                settings.shop_api_audit_retention_days,
            )
        )
        if settings.shop_api_enabled
        else None
    )
    payment_expiry_task = asyncio.create_task(
        payment_expiry_worker(
            session_factory,
            bot,
            settings.payment_expiry_sweep_seconds,
        )
    )
    notification_limiter = BroadcastRateLimiter(settings.broadcast_rate_per_second)
    broadcast_task = asyncio.create_task(
        broadcast_worker(
            session_factory,
            bot,
            rate_per_second=settings.broadcast_rate_per_second,
            limiter=notification_limiter,
            concurrency=settings.broadcast_concurrency,
            batch_size=settings.broadcast_batch_size,
        )
    )
    supplier_task = (
        asyncio.create_task(
            supplier_sync_worker(
                session_factory,
                supplier_client,
                settings.sumistore_sync_seconds,
                lehai_client,
            )
        )
        if supplier_client is not None
        else None
    )
    supplier_recovery_task = (
        asyncio.create_task(
            supplier_recovery_worker(
                session_factory,
                supplier_client,
                cipher,
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
    lehai_task = (
        asyncio.create_task(
            lehai_sync_worker(
                session_factory,
                lehai_client,
                settings.lehai_sync_seconds,
                supplier_client,
            )
        )
        if lehai_client is not None
        else None
    )
    lehai_audit_task = (
        asyncio.create_task(
            supplier_audit_worker(
                session_factory,
                lehai_client,
                bot,
                settings.admin_ids,
                settings.lehai_audit_seconds,
                provider="lehai",
                provider_label="Lê Hải Premium",
            )
        )
        if lehai_client is not None
        else None
    )
    rentsim_task = (
        asyncio.create_task(
            rentsim_otp_worker(
                session_factory,
                rentsim_client,
                bot,
                settings.admin_ids,
                settings.rentsim_poll_seconds,
                settings.referral_commission_percent,
                settings.rentsim_request_recovery_seconds,
                settings.rentsim_pending_alert_seconds,
            )
        )
        if rentsim_client is not None
        else None
    )
    sale_alert_task = asyncio.create_task(
        sale_alert_worker(
            session_factory,
            bot,
            limiter=notification_limiter,
            concurrency=settings.broadcast_concurrency,
            batch_size=settings.broadcast_batch_size,
        )
    )
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(bot)
    finally:
        startup_maintenance_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await startup_maintenance_task
        if api_audit_cleanup_task is not None:
            api_audit_cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await api_audit_cleanup_task
        broadcast_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await broadcast_task
        payment_expiry_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await payment_expiry_task
        if supplier_task is not None:
            supplier_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await supplier_task
        if supplier_recovery_task is not None:
            supplier_recovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await supplier_recovery_task
        if supplier_audit_task is not None:
            supplier_audit_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await supplier_audit_task
        if lehai_task is not None:
            lehai_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await lehai_task
        if lehai_audit_task is not None:
            lehai_audit_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await lehai_audit_task
        if rentsim_task is not None:
            rentsim_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await rentsim_task
        sale_alert_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sale_alert_task
        server.should_exit = True
        await api_task
        await storage.close()
        for external_client in (supplier_client, lehai_client, rentsim_client):
            if external_client is not None:
                await external_client.aclose()
        if deposit_notification_bot is not None:
            await deposit_notification_bot.session.close()
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
