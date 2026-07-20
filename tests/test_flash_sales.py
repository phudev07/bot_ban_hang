import asyncio
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import create_api
from app.broadcasts import deliver_pending_flash_sales, recover_interrupted_product_alerts
from app.config import Settings
from app.dashboard_security import hash_dashboard_password
from app.database import Base
from app.flash_sales import (
    FlashSaleUnavailable,
    consume_flash_sale,
    flash_sale_remaining,
    release_flash_sale_reservation,
    reserve_flash_sale,
)
from app.models import (
    Category,
    Deposit,
    DiscountCode,
    FlashSaleCampaign,
    InventoryItem,
    Order,
    Product,
    ProductAlertDelivery,
    QuantityDiscount,
    SupplierBalanceTransaction,
    User,
)
from app.payment_expiry import expire_pending_deposits
from app.price_alerts import apply_supplier_price
from app.services import create_deposit, process_sepay_payment, product_pricing, purchase_product
from app.suppliers import SupplierPurchase, SupplierSnapshot
from app.utils import SecretCipher


async def make_database(path: str = ":memory:"):
    url = "sqlite+aiosqlite:///:memory:" if path == ":memory:" else f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def seed_local_sale(
    sessions: async_sessionmaker,
    cipher: SecretCipher,
    *,
    quantity: int = 2,
) -> tuple[int, int, int]:
    async with sessions() as session:
        category = Category(name_vi="Flash", name_en="Flash")
        session.add(category)
        await session.flush()
        product = Product(
            category_id=category.id,
            name_vi="GPT Plus Flash",
            name_en="GPT Plus Flash",
            price=50_000,
            allow_quantity=True,
            max_quantity=10,
        )
        user = User(telegram_id=10001, full_name="Flash buyer", balance=500_000)
        session.add_all([product, user])
        await session.flush()
        session.add_all(
            InventoryItem(
                product_id=product.id,
                encrypted_secret=cipher.encrypt(f"flash-account-{index}"),
            )
            for index in range(quantity + 2)
        )
        campaign = FlashSaleCampaign(
            product_id=product.id,
            original_price=product.price,
            sale_price=30_000,
            total_quantity=quantity,
            message_text="Flash sale test",
        )
        session.add(campaign)
        await session.commit()
        return product.id, user.telegram_id, campaign.id


class ConcurrentFlashSupplier:
    provider = "sumistore"

    def __init__(
        self,
        *,
        unit_price: int = 15_000,
        snapshot_unit_price: int | None = None,
        stock: int = 20,
    ) -> None:
        self.balance_lock = asyncio.Lock()
        self.unit_price = unit_price
        self.snapshot_unit_price = snapshot_unit_price or unit_price
        self.stock = stock
        self.balance = unit_price * stock
        self.buy_count = 0
        self.in_flight = 0
        self.max_in_flight = 0

    async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
        return SupplierSnapshot(
            product_id=product_id,
            name="API Flash",
            description="Concurrent flash supplier",
            unit_price=self.snapshot_unit_price,
            source_stock=self.stock,
            owner_balance=self.balance,
        )

    async def buy(self, product_id: str, quantity: int) -> SupplierPurchase:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.005)
            self.buy_count += 1
            self.stock -= quantity
            self.balance -= self.unit_price * quantity
            return SupplierPurchase(
                order_code=f"FLASH-{self.buy_count}",
                unit_price=self.unit_price,
                accounts=tuple(
                    f"flash-{self.buy_count}-{index}|password"
                    for index in range(quantity)
                ),
                product_id=product_id,
                provider=self.provider,
            )
        finally:
            self.in_flight -= 1


def test_flash_price_does_not_stack_and_returns_to_normal_after_quota() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        product_id, user_id, campaign_id = await seed_local_sale(sessions, cipher)
        async with sessions() as session:
            session.add_all(
                [
                    DiscountCode(
                        product_id=product_id,
                        code="SAVE5K",
                        discount_type="fixed",
                        discount_value=5_000,
                    ),
                    QuantityDiscount(
                        product_id=product_id,
                        min_quantity=2,
                        discount_percent=20,
                    ),
                ]
            )
            await session.commit()
            product = await session.get(Product, product_id)
            assert product is not None
            pricing = await product_pricing(
                session,
                product,
                coupon_code="SAVE5K",
                quantity=2,
            )
            assert pricing is not None
            assert pricing.final_unit_price == 30_000
            assert pricing.coupon is None
            assert pricing.quantity_discount_percent == 0

        result = await purchase_product(
            sessions,
            user_id,
            product_id,
            cipher,
            quantity=2,
            coupon_code="SAVE5K",
        )
        assert result.ok is True
        assert result.total_amount == 60_000
        assert result.discount_amount == 40_000
        assert all(order.flash_sale_id == campaign_id for order in result.orders)

        async with sessions() as session:
            campaign = await session.get(FlashSaleCampaign, campaign_id)
            product = await session.get(Product, product_id)
            assert campaign is not None and campaign.status == "completed"
            assert campaign.sold_quantity == 2
            assert flash_sale_remaining(campaign) == 0
            assert product is not None
            normal = await product_pricing(session, product)
            assert normal is not None and normal.final_unit_price == 50_000

        normal_result = await purchase_product(sessions, user_id, product_id, cipher)
        assert normal_result.ok is True
        assert normal_result.total_amount == 50_000
        assert normal_result.orders[0].flash_sale_id is None
        await engine.dispose()

    asyncio.run(scenario())


def test_flash_quota_rejects_oversell_before_inventory_or_balance_changes() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        product_id, user_id, campaign_id = await seed_local_sale(
            sessions,
            cipher,
            quantity=1,
        )
        result = await purchase_product(
            sessions,
            user_id,
            product_id,
            cipher,
            quantity=2,
        )
        assert result.ok is False
        assert result.message == "out_of_stock"
        async with sessions() as session:
            campaign = await session.get(FlashSaleCampaign, campaign_id)
            user = await session.get(User, user_id)
            available = list(
                await session.scalars(
                    select(InventoryItem).where(InventoryItem.status == "available")
                )
            )
            assert campaign is not None and campaign.sold_quantity == 0
            assert user is not None and user.balance == 500_000
            assert len(available) == 3
            consume_flash_sale(campaign, 1)
            try:
                consume_flash_sale(campaign, 1)
            except FlashSaleUnavailable:
                pass
            else:
                raise AssertionError("Flash Sale quota allowed an oversell")
        await engine.dispose()

    asyncio.run(scenario())


def test_simultaneous_api_flash_buyers_never_fall_back_to_normal_price() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = ConcurrentFlashSupplier()
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="API Flash",
                name_en="API Flash",
                price=20_000,
                allow_quantity=True,
                max_quantity=10,
                fulfillment_source="sumistore",
                supplier_product_id="SP-CONCURRENT-FLASH",
                supplier_price=15_000,
                supplier_markup=5_000,
                supplier_synced_at=datetime.now(UTC),
                external_stock=20,
            )
            session.add(product)
            await session.flush()
            campaign = FlashSaleCampaign(
                product_id=product.id,
                original_price=20_000,
                sale_price=17_000,
                total_quantity=3,
                message_text="Concurrent API sale",
            )
            session.add(campaign)
            session.add_all(
                User(
                    telegram_id=20_000 + index,
                    full_name=f"Flash buyer {index}",
                    balance=20_000,
                )
                for index in range(100)
            )
            await session.commit()
            product_id = product.id
            campaign_id = campaign.id

        results = await asyncio.gather(
            *(
                purchase_product(
                    sessions,
                    20_000 + index,
                    product_id,
                    cipher,
                    supplier_client=supplier,  # type: ignore[arg-type]
                    supplier_idempotency_key=f"flash-concurrent-{index}",
                    expected_flash_sale_id=campaign_id,
                )
                for index in range(100)
            )
        )

        completed = [result for result in results if result.ok]
        rejected = [result for result in results if not result.ok]
        assert len(completed) == 3
        assert len(rejected) == 97
        assert {result.message for result in rejected} == {"flash_sale_unavailable"}
        assert all(result.total_amount == 17_000 for result in completed)
        assert supplier.buy_count == 3
        assert supplier.max_in_flight == 1

        async with sessions() as session:
            campaign = await session.get(FlashSaleCampaign, campaign_id)
            orders = list(await session.scalars(select(Order).order_by(Order.id)))
            users = list(await session.scalars(select(User).order_by(User.telegram_id)))
            assert campaign is not None
            assert campaign.sold_quantity == 3
            assert campaign.reserved_quantity == 0
            assert campaign.status == "completed"
            assert len(orders) == 3
            assert {order.amount for order in orders} == {17_000}
            assert sorted(user.balance for user in users) == [3_000] * 3 + [20_000] * 97
        await engine.dispose()

    asyncio.run(scenario())


def test_supplier_cost_spike_preserves_accounts_and_does_not_charge_flash_buyer() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = ConcurrentFlashSupplier(
            unit_price=13_000,
            snapshot_unit_price=10_000,
            stock=1,
        )
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="API Cost Spike",
                name_en="API Cost Spike",
                price=15_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-COST-SPIKE",
                supplier_price=10_000,
                supplier_markup=5_000,
                supplier_synced_at=datetime.now(UTC),
                external_stock=1,
            )
            user = User(telegram_id=30_001, full_name="Protected buyer", balance=20_000)
            session.add_all([product, user])
            await session.flush()
            campaign = FlashSaleCampaign(
                product_id=product.id,
                original_price=15_000,
                sale_price=12_000,
                total_quantity=1,
                message_text="Cost guard sale",
            )
            session.add(campaign)
            await session.commit()
            product_id = product.id
            campaign_id = campaign.id

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product_id,
            cipher,
            supplier_client=supplier,  # type: ignore[arg-type]
            expected_flash_sale_id=campaign_id,
        )

        assert result.ok is False
        assert result.message == "flash_sale_unavailable"
        async with sessions() as session:
            protected_user = await session.get(User, user.telegram_id)
            campaign = await session.get(FlashSaleCampaign, campaign_id)
            product = await session.get(Product, product_id)
            item = await session.scalar(select(InventoryItem))
            order = await session.scalar(select(Order))
            supplier_transaction = await session.scalar(select(SupplierBalanceTransaction))
            assert protected_user is not None and protected_user.balance == 20_000
            assert campaign is not None and campaign.status == "cost_exceeded"
            assert campaign.sold_quantity == 0
            assert product is not None and product.supplier_price == 13_000
            assert product.price == 18_000
            assert item is not None and item.status == "available"
            assert cipher.decrypt(item.encrypted_secret).startswith("flash-1-")
            assert order is None
            assert supplier_transaction is not None
            assert supplier_transaction.amount == -13_000
            assert supplier_transaction.shop_order_code.startswith("R")
        await engine.dispose()

    asyncio.run(scenario())


def test_pending_qr_reserves_once_and_expiry_reopens_flash_sale() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        product_id, user_id, campaign_id = await seed_local_sale(
            sessions,
            cipher,
            quantity=1,
        )
        async with sessions() as session:
            first = await create_deposit(
                session,
                user_id,
                30_000,
                payment_kind="direct_purchase",
                product_id=product_id,
                flash_sale_id=campaign_id,
                flash_sale_quantity=1,
                expiry_seconds=300,
            )
            repeated = await create_deposit(
                session,
                user_id,
                30_000,
                payment_kind="direct_purchase",
                product_id=product_id,
                flash_sale_id=campaign_id,
                flash_sale_quantity=1,
                expiry_seconds=300,
            )
            assert repeated.id == first.id
            first.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

        async with sessions() as session:
            campaign = await session.get(FlashSaleCampaign, campaign_id)
            assert campaign is not None
            assert campaign.reserved_quantity == 1
            assert campaign.status == "completed"

        assert await expire_pending_deposits(sessions) == 1
        async with sessions() as session:
            campaign = await session.get(FlashSaleCampaign, campaign_id)
            deposit = await session.scalar(select(Deposit))
            assert campaign is not None and campaign.reserved_quantity == 0
            assert campaign.status == "active"
            assert deposit is not None and deposit.flash_sale_quantity == 0
            assert deposit.status == "failed"
        await engine.dispose()

    asyncio.run(scenario())


def test_paid_qr_moves_flash_reservation_to_sold() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        product_id, user_id, campaign_id = await seed_local_sale(
            sessions,
            cipher,
            quantity=1,
        )
        async with sessions() as session:
            deposit = await create_deposit(
                session,
                user_id,
                30_000,
                payment_kind="direct_purchase",
                product_id=product_id,
                flash_sale_id=campaign_id,
                flash_sale_quantity=1,
                expiry_seconds=300,
            )
            code = deposit.code

        result = await process_sepay_payment(
            sessions,
            {
                "id": "FLASH-QR-1",
                "transferType": "in",
                "transferAmount": 30_000,
                "content": code,
            },
            cipher=cipher,
        )
        assert result.status == "direct_purchase_completed"
        async with sessions() as session:
            campaign = await session.get(FlashSaleCampaign, campaign_id)
            order = await session.scalar(select(Order))
            assert campaign is not None
            assert campaign.reserved_quantity == 0
            assert campaign.sold_quantity == 1
            assert campaign.status == "completed"
            assert order is not None and order.flash_sale_id == campaign_id
        await engine.dispose()

    asyncio.run(scenario())


def test_flash_qr_cost_spike_refunds_wallet_and_preserves_supplier_account() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = ConcurrentFlashSupplier(
            unit_price=13_000,
            snapshot_unit_price=10_000,
            stock=1,
        )
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Flash QR Cost Spike",
                name_en="Flash QR Cost Spike",
                price=15_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-QR-COST-SPIKE",
                supplier_price=10_000,
                supplier_markup=5_000,
                supplier_synced_at=datetime.now(UTC),
                external_stock=1,
            )
            user = User(telegram_id=31_001, full_name="QR buyer", balance=0)
            session.add_all([product, user])
            await session.flush()
            campaign = FlashSaleCampaign(
                product_id=product.id,
                original_price=15_000,
                sale_price=12_000,
                total_quantity=1,
                message_text="QR cost guard",
            )
            session.add(campaign)
            await session.flush()
            await session.commit()
            product_id = product.id
            campaign_id = campaign.id

        async with sessions() as session:
            deposit = await create_deposit(
                session,
                user.telegram_id,
                12_000,
                payment_kind="direct_purchase",
                product_id=product_id,
                flash_sale_id=campaign_id,
                flash_sale_quantity=1,
            )
            deposit_code = deposit.code

        result = await process_sepay_payment(
            sessions,
            {
                "id": "FLASH-QR-COST-SPIKE",
                "transferType": "in",
                "transferAmount": 12_000,
                "content": deposit_code,
            },
            cipher=cipher,
            supplier_client=supplier,  # type: ignore[arg-type]
        )

        assert result.status == "direct_purchase_fallback"
        assert result.balance == 12_000
        async with sessions() as session:
            user_after = await session.get(User, user.telegram_id)
            campaign = await session.get(FlashSaleCampaign, campaign_id)
            deposit = await session.scalar(select(Deposit))
            item = await session.scalar(select(InventoryItem))
            assert user_after is not None and user_after.balance == 12_000
            assert campaign is not None and campaign.status == "cost_exceeded"
            assert campaign.reserved_quantity == 0
            assert campaign.sold_quantity == 0
            assert deposit is not None and deposit.flash_sale_quantity == 0
            assert item is not None and item.status == "available"
            assert await session.scalar(select(Order)) is None
        await engine.dispose()

    asyncio.run(scenario())


def test_supplier_cost_increase_stops_flash_sale() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="API Flash",
                name_en="API Flash",
                price=15_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-FLASH",
                supplier_price=10_000,
                supplier_markup=5_000,
                supplier_synced_at=datetime.now(UTC),
                external_stock=10,
            )
            session.add(product)
            await session.flush()
            campaign = FlashSaleCampaign(
                product_id=product.id,
                original_price=15_000,
                sale_price=12_000,
                total_quantity=5,
                message_text="API sale",
            )
            session.add(campaign)
            await session.commit()

            await apply_supplier_price(session, product, 12_001)
            await session.commit()
            await session.refresh(campaign)
            assert campaign.status == "cost_exceeded"
            assert campaign.notification_status == "superseded"
            assert campaign.ended_at is not None
        await engine.dispose()

    asyncio.run(scenario())


def test_reserved_final_slot_does_not_reopen_after_supplier_cost_exceeds_sale() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Reserved API Flash",
                name_en="Reserved API Flash",
                price=15_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-RESERVED-FLASH",
                supplier_price=10_000,
                supplier_markup=5_000,
                supplier_synced_at=datetime.now(UTC),
                external_stock=1,
            )
            session.add(product)
            await session.flush()
            campaign = FlashSaleCampaign(
                product_id=product.id,
                original_price=15_000,
                sale_price=12_000,
                total_quantity=1,
                message_text="Reserved sale",
            )
            session.add(campaign)
            await session.flush()
            reserve_flash_sale(campaign, 1)
            assert campaign.status == "completed"

            await apply_supplier_price(session, product, 12_001)
            assert campaign.status == "cost_exceeded"
            release_flash_sale_reservation(campaign, 1)
            assert campaign.reserved_quantity == 0
            assert campaign.status == "cost_exceeded"
        await engine.dispose()

    asyncio.run(scenario())


class FakeFlashBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.photos: list[tuple[int, str, str]] = []
        self.deleted: list[tuple[int, int]] = []

    async def send_message(self, chat_id: int, text: str, **_kwargs) -> None:
        self.messages.append((chat_id, text))

    async def send_photo(self, chat_id: int, photo, **kwargs):
        if isinstance(photo, str):
            self.photos.append((chat_id, photo, kwargs.get("caption", "")))
            return None
        return SimpleNamespace(
            photo=[SimpleNamespace(file_id="telegram-flash-photo")],
            message_id=77,
        )

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))


def test_flash_notification_uses_durable_delivery_and_photo_file_id() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="Flash", name_en="Flash")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Photo Flash",
                name_en="Photo Flash",
                price=50_000,
            )
            session.add(product)
            await session.flush()
            campaign = FlashSaleCampaign(
                product_id=product.id,
                original_price=50_000,
                sale_price=30_000,
                total_quantity=3,
                message_text="⚡ <b>Thông báo riêng</b>",
                telegram_photo_file_id="telegram-photo-id",
            )
            session.add(campaign)
            session.add_all(
                [
                    User(telegram_id=1, full_name="One", has_started=True),
                    User(telegram_id=2, full_name="Two", has_started=True),
                ]
            )
            await session.commit()
            campaign_id = campaign.id

        bot = FakeFlashBot()
        assert await deliver_pending_flash_sales(sessions, bot) == 1
        assert [call[0] for call in bot.photos] == [1, 2]
        assert all(call[1] == "telegram-photo-id" for call in bot.photos)
        assert all(call[2] == "⚡ <b>Thông báo riêng</b>" for call in bot.photos)
        async with sessions() as session:
            campaign = await session.get(FlashSaleCampaign, campaign_id)
            deliveries = list(await session.scalars(select(ProductAlertDelivery)))
            assert campaign is not None and campaign.notification_status == "sent"
            assert campaign.delivered_count == 2
            assert all(delivery.status == "sent" for delivery in deliveries)
        await engine.dispose()

    asyncio.run(scenario())


def test_flash_notification_restart_only_sends_unfinished_recipients() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="Flash", name_en="Flash")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Resume",
                name_en="Resume",
                price=50_000,
            )
            session.add(product)
            await session.flush()
            campaign = FlashSaleCampaign(
                product_id=product.id,
                original_price=50_000,
                sale_price=30_000,
                total_quantity=3,
                message_text="Resume sale",
                notification_status="sending",
                total_recipients=2,
            )
            session.add(campaign)
            session.add_all(
                [
                    User(telegram_id=1, full_name="Done", has_started=True),
                    User(telegram_id=2, full_name="Pending", has_started=True),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    ProductAlertDelivery(
                        alert_type="flash",
                        alert_id=campaign.id,
                        user_id=1,
                        status="sent",
                    ),
                    ProductAlertDelivery(
                        alert_type="flash",
                        alert_id=campaign.id,
                        user_id=2,
                        status="sending",
                    ),
                ]
            )
            await session.commit()

        await recover_interrupted_product_alerts(sessions)
        bot = FakeFlashBot()
        assert await deliver_pending_flash_sales(sessions, bot) == 1
        assert bot.messages == [(2, "Resume sale")]
        await engine.dispose()

    asyncio.run(scenario())


def test_admin_flash_sale_page_creation_cost_guard_and_image_upload(tmp_path) -> None:
    async def setup_database():
        engine, sessions = await make_database((tmp_path / "flash-admin.db").as_posix())
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Admin Flash API",
                name_en="Admin Flash API",
                price=15_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-ADMIN-FLASH",
                supplier_price=10_000,
                supplier_markup=5_000,
                external_stock=5,
            )
            session.add(product)
            await session.commit()
            return engine, sessions, product.id

    engine, sessions, product_id = asyncio.run(setup_database())
    encryption_key = Fernet.generate_key().decode()
    settings = Settings(
        _env_file=None,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        inventory_encryption_key=encryption_key,
        dashboard_enabled=True,
        dashboard_username="admin",
        dashboard_password_hash=hash_dashboard_password("dashboard-password"),
        dashboard_session_secret="session-secret-long-enough-for-tests",
        ADMIN_IDS="999",
    )
    bot = FakeFlashBot()
    app = create_api(
        settings,
        sessions,
        bot,  # type: ignore[arg-type]
        SecretCipher(encryption_key),
    )

    with TestClient(app, base_url="https://testserver") as client:
        client.post(
            "/admin/login",
            data={"username": "admin", "password": "dashboard-password"},
        )
        page = client.get("/admin/flash-sales")
        assert page.status_code == 200
        assert "Mở chiến dịch" in page.text
        assert "Admin Flash API" in page.text
        assert 'href="/admin/flash-sales"' in page.text
        token_match = re.search(r'name="csrf" value="([^"]+)"', page.text)
        assert token_match is not None
        csrf = token_match.group(1)

        below_cost = client.post(
            "/admin/flash-sales",
            data={
                "csrf": csrf,
                "product_id": product_id,
                "sale_price": "9999",
                "total_quantity": 2,
                "message_text": "Không được tạo",
            },
            follow_redirects=True,
        )
        assert "không được thấp hơn giá vốn API" in below_cost.text

        created = client.post(
            "/admin/flash-sales",
            data={
                "csrf": csrf,
                "product_id": product_id,
                "sale_price": "12000",
                "total_quantity": 2,
                "message_text": "⚡ Flash riêng của shop",
            },
            files={"image": ("flash.png", b"fake-image-content", "image/png")},
            follow_redirects=True,
        )
        assert created.status_code == 200
        assert "Đã bật Flash Sale" in created.text
        assert "Flash riêng của shop" in created.text
        assert bot.deleted == [(999, 77)]

    async def verify() -> None:
        async with sessions() as session:
            campaigns = list(await session.scalars(select(FlashSaleCampaign)))
            assert len(campaigns) == 1
            assert campaigns[0].sale_price == 12_000
            assert campaigns[0].telegram_photo_file_id == "telegram-flash-photo"

    asyncio.run(verify())
    asyncio.run(engine.dispose())
