import asyncio
from datetime import UTC, datetime, timedelta
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import (
    BalanceAdjustment,
    Category,
    Deposit,
    DiscountCode,
    InventoryItem,
    Order,
    PaymentTransaction,
    Product,
    ProductPriceAlert,
    QuantityDiscount,
    ReferralReward,
    SellerPrice,
    SupplierRecoveryRequest,
    User,
    WalletTransaction,
)
from app.services import (
    approve_direct_purchase_deposit,
    approve_wallet_deposit,
    active_products,
    available_stock,
    cancel_direct_purchase_deposit,
    cancel_wallet_deposit,
    CouponValidationError,
    create_deposit,
    customer_product_prices,
    multi_supplier_quote,
    order_bundle,
    process_sepay_payment,
    process_binance_payment,
    product_checkout_quote,
    product_pricing,
    ProductPricing,
    purchase_quantity_limit,
    purchase_product,
    recent_orders,
    user_activity_stats,
)
from app.suppliers import SupplierError, SupplierPurchase, SupplierSnapshot
from app.utils import SecretCipher


async def make_database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_purchase_quantity_limit_never_exceeds_current_stock() -> None:
    product = Product(max_quantity=100)

    assert purchase_quantity_limit(product, 24) == 24
    assert purchase_quantity_limit(product, 150) == 100
    assert purchase_quantity_limit(product, 0) == 0


def test_binance_deposit_credits_usd_wallet_without_vnd_conversion() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            user = User(telegram_id=72001, full_name="USD buyer")
            session.add(user)
            await session.commit()
            deposit = await create_deposit(
                session,
                user.telegram_id,
                10,
                "BN",
                payment_kind="binance",
                currency="USD",
            )
            result = await process_binance_payment(
                sessions,
                {
                    "bizStatus": "PAY_SUCCESS",
                    "merchantTradeNo": deposit.code,
                    "bizId": "binance-tx-1",
                    "orderAmount": {"currency": "USDT", "total": "1.00000000"},
                },
                "BN",
                usd_to_vnd=27_500,
            )
        async with sessions() as session:
            stored_user = await session.get(User, user.telegram_id)
            transaction = await session.scalar(select(PaymentTransaction))
            assert result.status == "credited"
            assert result.currency == "USD"
            assert result.amount == 10
            assert stored_user is not None
            assert stored_user.balance == 0
            assert stored_user.balance_usd_tenths == 10
            assert transaction is not None
            assert transaction.currency == "USD"
        await engine.dispose()

    asyncio.run(scenario())


def test_submitted_binance_transaction_id_is_reused_for_settlement_once() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            user = User(telegram_id=72002, full_name="USD replay test")
            session.add(user)
            await session.commit()
            deposit = await create_deposit(
                session,
                user.telegram_id,
                10,
                "BN",
                payment_kind="binance",
                currency="USD",
            )
            session.add(
                PaymentTransaction(
                    deposit_id=deposit.id,
                    user_id=user.telegram_id,
                    provider_tx_id="pay-replay-1",
                    amount=0,
                    currency="USD",
                    credit_status="submitted",
                )
            )
            await session.commit()
        result = await process_sepay_payment(
            sessions,
            {
                "id": "pay-replay-1",
                "amount": 10,
                "code": deposit.code,
                "transferType": "in",
            },
            "BN",
            currency="USD",
        )
        async with sessions() as session:
            transaction = await session.scalar(select(PaymentTransaction))
            stored_user = await session.get(User, user.telegram_id)
            assert result.status == "credited"
            assert transaction is not None and transaction.credit_status == "credited"
            assert stored_user is not None and stored_user.balance_usd_tenths == 10
        await engine.dispose()

    asyncio.run(scenario())


def test_seller_price_uses_each_inventory_cost_and_keeps_normal_price_unchanged() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="Seller", name_en="Seller")
            seller = User(telegram_id=71001, full_name="Seller", balance=100_000)
            normal = User(telegram_id=71002, full_name="Normal", balance=100_000)
            session.add_all([category, seller, normal])
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT seller",
                name_en="GPT seller",
                price=40_000,
                allow_quantity=True,
                max_quantity=10,
            )
            session.add(product)
            await session.flush()
            rule = SellerPrice(
                user_id=seller.telegram_id,
                product_id=product.id,
                profit_per_unit=5_000,
            )
            session.add(rule)
            session.add_all(
                [
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt("seller-1|password"),
                        cost_amount=30_000,
                    ),
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt("seller-2|password"),
                        cost_amount=31_000,
                    ),
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt("normal-1|password"),
                        cost_amount=30_000,
                    ),
                ]
            )
            await session.commit()

        async with sessions() as session:
            quoted_product = await session.get(Product, product.id)
            assert quoted_product is not None
            pricing = await product_pricing(
                session,
                quoted_product,
                quantity=2,
                user_id=seller.telegram_id,
            )
            assert pricing is not None
            quote = await product_checkout_quote(
                session,
                quoted_product,
                2,
                pricing,
                None,
                None,
            )
            assert quote.unit_prices == (35_000, 36_000)
            assert quote.total_amount == 71_000

        stale_quote_result = await purchase_product(
            sessions,
            seller.telegram_id,
            product.id,
            cipher,
            quantity=2,
            expected_total_amount=70_000,
        )
        seller_result = await purchase_product(
            sessions,
            seller.telegram_id,
            product.id,
            cipher,
            quantity=2,
            expected_total_amount=71_000,
        )
        normal_result = await purchase_product(
            sessions,
            normal.telegram_id,
            product.id,
            cipher,
        )

        assert stale_quote_result.ok is False
        assert stale_quote_result.message == "price_changed"
        assert stale_quote_result.total_amount == 71_000
        assert seller_result.ok is True
        assert seller_result.total_amount == 71_000
        assert [order.amount for order in seller_result.orders] == [35_000, 36_000]
        assert all(order.seller_price_id == rule.id for order in seller_result.orders)
        assert all(order.seller_profit_per_unit == 5_000 for order in seller_result.orders)
        assert normal_result.ok is True
        assert normal_result.total_amount == 40_000
        assert normal_result.orders[0].seller_price_id is None
        async with sessions() as session:
            stored_seller = await session.get(User, seller.telegram_id)
            stored_normal = await session.get(User, normal.telegram_id)
            assert stored_seller is not None and stored_seller.balance == 29_000
            assert stored_normal is not None and stored_normal.balance == 60_000
        await engine.dispose()

    asyncio.run(scenario())


def test_seller_price_falls_back_to_public_price_when_cost_makes_it_unsafe() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="Seller", name_en="Seller")
            seller = User(telegram_id=72001, full_name="Seller")
            session.add_all([category, seller])
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Unsafe seller price",
                name_en="Unsafe seller price",
                price=40_000,
            )
            session.add(product)
            await session.flush()
            session.add_all(
                [
                    SellerPrice(
                        user_id=seller.telegram_id,
                        product_id=product.id,
                        profit_per_unit=5_000,
                    ),
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret="encrypted",
                        cost_amount=35_000,
                    ),
                ]
            )
            await session.commit()

        async with sessions() as session:
            product = await session.get(Product, product.id)
            assert product is not None
            pricing = await product_pricing(
                session,
                product,
                user_id=seller.telegram_id,
            )
            prices = await customer_product_prices(
                session,
                [product],
                seller.telegram_id,
            )
            assert pricing is not None
            assert pricing.seller_price_id is None
            assert pricing.final_unit_price == 40_000
            assert prices[product.id] == 40_000
        await engine.dispose()

    asyncio.run(scenario())


def test_seller_purchase_falls_back_to_public_price_for_unsafe_later_item() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="Seller", name_en="Seller")
            seller = User(telegram_id=72002, full_name="Seller", balance=100_000)
            session.add_all([category, seller])
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Mixed seller costs",
                name_en="Mixed seller costs",
                price=40_000,
                allow_quantity=True,
            )
            session.add(product)
            await session.flush()
            session.add(
                SellerPrice(
                    user_id=seller.telegram_id,
                    product_id=product.id,
                    profit_per_unit=5_000,
                )
            )
            session.add_all(
                [
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt("safe|password"),
                        cost_amount=30_000,
                    ),
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt("unsafe|password"),
                        cost_amount=38_000,
                    ),
                ]
            )
            await session.commit()

        result = await purchase_product(
            sessions,
            seller.telegram_id,
            product.id,
            cipher,
            quantity=2,
            expected_total_amount=80_000,
        )

        assert result.ok is True
        assert result.total_amount == 80_000
        assert [order.amount for order in result.orders] == [40_000, 40_000]
        assert all(order.seller_price_id is None for order in result.orders)
        async with sessions() as session:
            stored_seller = await session.get(User, seller.telegram_id)
            assert stored_seller is not None and stored_seller.balance == 20_000
        await engine.dispose()

    asyncio.run(scenario())


def test_quick_buy_orders_all_gpt_products_before_google_products() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            google = Category(
                name_vi="Gemini / Veo3 / Antigravity",
                name_en="Google",
                position=0,
            )
            gpt = Category(name_vi="Tài Khoản ChatGPT cá nhân", name_en="ChatGPT", position=99)
            session.add_all([google, gpt])
            await session.flush()
            session.add_all(
                [
                    Product(
                        category_id=google.id,
                        name_vi="Link GG Pro Jio 18M",
                        name_en="Google Pro Jio 18M",
                        price=20_000,
                    ),
                    Product(
                        category_id=gpt.id,
                        name_vi="GPT Plus",
                        name_en="GPT Plus",
                        price=40_000,
                    ),
                    Product(
                        category_id=gpt.id,
                        name_vi="GPT trắng",
                        name_en="New ChatGPT account",
                        price=6_000,
                    ),
                ]
            )
            await session.commit()

        async with sessions() as session:
            products = await active_products(session)
            assert [product.name_vi for product in products] == [
                "GPT Plus",
                "GPT trắng",
                "Link GG Pro Jio 18M",
            ]
        await engine.dispose()

    asyncio.run(scenario())


def test_quick_buy_moves_sold_out_products_below_available_products() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            google = Category(name_vi="Gemini", name_en="Google", position=0)
            gpt = Category(name_vi="ChatGPT", name_en="ChatGPT", position=1)
            session.add_all([google, gpt])
            await session.flush()
            session.add_all(
                [
                    Product(
                        category_id=gpt.id,
                        name_vi="GPT còn hàng",
                        name_en="GPT available",
                        price=40_000,
                        fulfillment_source="sumistore",
                        external_stock=2,
                    ),
                    Product(
                        category_id=gpt.id,
                        name_vi="GPT hết hàng",
                        name_en="GPT sold out",
                        price=40_000,
                        fulfillment_source="sumistore",
                        external_stock=0,
                    ),
                    Product(
                        category_id=google.id,
                        name_vi="GG còn hàng",
                        name_en="Google available",
                        price=20_000,
                        fulfillment_source="sumistore",
                        external_stock=5,
                    ),
                ]
            )
            await session.commit()

        async with sessions() as session:
            products = await active_products(session)
            assert [product.name_vi for product in products] == [
                "GPT còn hàng",
                "GG còn hàng",
                "GPT hết hàng",
            ]
        await engine.dispose()

    asyncio.run(scenario())


def test_quick_buy_uses_available_inventory_for_local_product_stock() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            local_product = Product(
                category_id=category.id,
                name_vi="GPT kho nhập",
                name_en="Local GPT",
                price=40_000,
                fulfillment_source="local",
                external_stock=0,
            )
            sold_out_product = Product(
                category_id=category.id,
                name_vi="GPT hết hàng",
                name_en="Sold-out GPT",
                price=40_000,
                fulfillment_source="local",
                external_stock=10,
            )
            session.add_all([local_product, sold_out_product])
            await session.flush()
            session.add(
                InventoryItem(
                    product_id=local_product.id,
                    encrypted_secret="encrypted",
                    status="available",
                )
            )
            await session.commit()

        async with sessions() as session:
            products = await active_products(session)
            assert [product.name_vi for product in products] == [
                "GPT kho nhập",
                "GPT hết hàng",
            ]
            assert products[0]._menu_stock == 1
            assert products[1]._menu_stock == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_quick_buy_includes_haji_claude_account_products() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            session.add(
                Product(
                    category_id=category.id,
                    name_vi="Claude Team Standard 1 tháng",
                    name_en="Claude Team Standard 1 month",
                    price=450_000,
                    product_type="account",
                    fulfillment_source="haji",
                    supplier_product_id="claude_addteam1x25",
                    external_stock=2,
                )
            )
            await session.commit()

        async with sessions() as session:
            products = await active_products(session)
            assert [product.supplier_product_id for product in products] == [
                "claude_addteam1x25"
            ]
            assert products[0]._menu_stock == 2
        await engine.dispose()

    asyncio.run(scenario())


class FakeSupplier:
    def __init__(self, *, balance: int = 100_000, stock: int = 100) -> None:
        self.balance = balance
        self.stock = stock
        self.buy_calls = 0
        self.buy_quantities: list[int] = []
        self.fetch_calls = 0

    async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
        self.fetch_calls += 1
        return SupplierSnapshot(
            product_id=product_id,
            name="ChatGPT Plus",
            description="Supplier product",
            unit_price=15_000,
            source_stock=self.stock,
            owner_balance=self.balance,
        )

    async def buy(self, product_id: str, quantity: int) -> SupplierPurchase:
        self.buy_calls += 1
        self.buy_quantities.append(quantity)
        return SupplierPurchase(
            order_code="API-TELE-TEST123",
            unit_price=15_000,
            accounts=tuple(f"chatgpt{index}:password" for index in range(1, quantity + 1)),
        )


def test_seller_purchase_uses_local_stock_then_buys_only_missing_quantity() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = FakeSupplier(balance=1_000_000, stock=100)
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            seller = User(telegram_id=73001, full_name="Seller", balance=100_000)
            session.add_all([category, seller])
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Mixed local API",
                name_en="Mixed local API",
                price=28_000,
                allow_quantity=True,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_price=15_000,
                supplier_markup=13_000,
                external_stock=2,
            )
            session.add(product)
            await session.flush()
            session.add_all(
                [
                    SellerPrice(
                        user_id=seller.telegram_id,
                        product_id=product.id,
                        profit_per_unit=5_000,
                    ),
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt("local|password"),
                        cost_amount=20_000,
                    ),
                ]
            )
            await session.commit()

        result = await purchase_product(
            sessions,
            seller.telegram_id,
            product.id,
            cipher,
            quantity=2,
            supplier_client=supplier,  # type: ignore[arg-type]
            expected_total_amount=45_000,
        )

        assert result.ok is True
        assert result.total_amount == 45_000
        assert supplier.buy_quantities == [1]
        assert result.secrets == ["local|password", "chatgpt1:password"]
        assert [order.amount for order in result.orders] == [25_000, 20_000]
        assert [order.cost_amount for order in result.orders] == [20_000, 15_000]
        async with sessions() as session:
            stored_seller = await session.get(User, seller.telegram_id)
            available_items = int(
                await session.scalar(
                    select(func.count(InventoryItem.id)).where(
                        InventoryItem.product_id == product.id,
                        InventoryItem.status == "available",
                    )
                )
                or 0
            )
            assert stored_seller is not None and stored_seller.balance == 55_000
            assert available_items == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_qr_quote_uses_local_inventory_without_calling_supplier() -> None:
    async def scenario() -> None:
        product = Product(
            fulfillment_source="sumistore",
            supplier_product_id="SP-GEF55PBV",
            sumistore_api_enabled=True,
            lehai_api_enabled=False,
            supplier_markup=5_000,
            price=35_000,
        )
        client = FakeSupplier(balance=0, stock=100)
        pricing = ProductPricing(
            original_unit_price=35_000,
            discount_per_unit=0,
            final_unit_price=35_000,
        )

        quote = await multi_supplier_quote(
            product,
            1,
            pricing,
            client,  # type: ignore[arg-type]
            None,
            local_stock=1,
        )

        assert quote is None
        assert client.fetch_calls == 0

    asyncio.run(scenario())


class RoutedSupplier:
    def __init__(
        self,
        provider: str,
        *,
        unit_price: int,
        stock: int,
        balance: int,
    ) -> None:
        self.provider = provider
        self.unit_price = unit_price
        self.stock = stock
        self.balance = balance
        self.balance_lock = asyncio.Lock()
        self.buy_quantities: list[int] = []
        self.fetch_product_ids: list[str] = []

    async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
        self.fetch_product_ids.append(product_id)
        return SupplierSnapshot(
            product_id=product_id,
            name="Routed GPT Plus",
            description="",
            unit_price=self.unit_price,
            source_stock=self.stock,
            owner_balance=self.balance,
        )

    async def buy(
        self,
        product_id: str,
        quantity: int,
        *,
        idempotency_key: str | None = None,
    ) -> SupplierPurchase:
        del idempotency_key
        if quantity > min(self.stock, self.balance // self.unit_price):
            raise SupplierError("INSUFFICIENT_STOCK")
        self.buy_quantities.append(quantity)
        self.stock -= quantity
        self.balance -= quantity * self.unit_price
        call_number = len(self.buy_quantities)
        return SupplierPurchase(
            order_code=f"{self.provider.upper()}-{call_number}",
            unit_price=self.unit_price,
            accounts=tuple(
                f"{self.provider}-{call_number}-{index}|password"
                for index in range(1, quantity + 1)
            ),
            product_id=product_id,
            provider=self.provider,
        )


def test_external_stock_ui_refresh_uses_short_cache() -> None:
    class CountingSupplier:
        provider = "sumistore"

        def __init__(self) -> None:
            self.calls = 0

        async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
            self.calls += 1
            return SupplierSnapshot(
                product_id=product_id,
                name="Cached product",
                description="",
                unit_price=15_000,
                source_stock=8,
                owner_balance=150_000,
            )

    async def scenario() -> None:
        engine, sessions = await make_database()
        supplier = CountingSupplier()
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Cached product",
                name_en="Cached product",
                price=20_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-CACHED",
                supplier_price=15_000,
                supplier_markup=5_000,
                external_stock=7,
                supplier_synced_at=datetime.now(UTC),
            )
            session.add(product)
            await session.commit()
            product_id = product.id

        async with sessions() as session:
            assert (
                await available_stock(
                    session,
                    product_id,
                    supplier,  # type: ignore[arg-type]
                    refresh_external=True,
                    refresh_max_age_seconds=10,
                )
                == 7
            )
            assert supplier.calls == 0
            product = await session.get(Product, product_id)
            assert product is not None
            product.supplier_synced_at = datetime.now(UTC) - timedelta(seconds=11)
            await session.commit()

        async with sessions() as session:
            assert (
                await available_stock(
                    session,
                    product_id,
                    supplier,  # type: ignore[arg-type]
                    refresh_external=True,
                    refresh_max_age_seconds=10,
                )
                == 8
            )
            assert (
                await available_stock(
                    session,
                    product_id,
                    supplier,  # type: ignore[arg-type]
                    refresh_external=True,
                    refresh_max_age_seconds=10,
                )
                == 8
            )
            assert supplier.calls == 1
        await engine.dispose()

    asyncio.run(scenario())


class TimeoutRecoveringSupplier(FakeSupplier):
    async def buy(self, product_id: str, quantity: int) -> SupplierPurchase:
        self.buy_calls += 1
        raise SupplierError("SUPPLIER_UNAVAILABLE")

    async def recover_recent_purchase(
        self,
        product_id: str,
        quantity: int,
        **_kwargs,
    ) -> SupplierPurchase:
        return SupplierPurchase(
            order_code="API-TELE-RECOVERED",
            unit_price=15_000,
            accounts=tuple(
                f"recovered{index}:password" for index in range(1, quantity + 1)
            ),
            product_id=product_id,
        )


class PendingRecoverySupplier(FakeSupplier):
    provider = "sumistore"

    async def buy(self, product_id: str, quantity: int) -> SupplierPurchase:
        self.buy_calls += 1
        raise SupplierError("SUPPLIER_UNAVAILABLE")

    async def recover_recent_purchase(
        self,
        product_id: str,
        quantity: int,
        **_kwargs,
    ) -> None:
        return None


def test_purchase_is_atomic_and_delivers_stock() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Item",
                name_en="Item",
                price=50_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=100_000)
            session.add_all([product, user])
            await session.flush()
            item = InventoryItem(
                product_id=product.id,
                encrypted_secret=cipher.encrypt("account:password"),
                import_note="Nguồn test nội bộ",
            )
            session.add(item)
            await session.commit()

            result = await purchase_product(sessions, user.telegram_id, product.id, cipher)
            assert result.ok is True
            assert result.secret == "account:password"
            assert result.orders[0].product_name_vi == "Item"
            assert result.orders[0].product_name_en == "Item"
            assert result.orders[0].inventory_import_note == "Nguồn test nội bộ"

            product.name_vi = "Renamed item"
            product.name_en = "Renamed item"
            assert result.orders[0].display_name_vi == "Item"
            assert result.orders[0].display_name_en == "Item"

            await session.refresh(user)
            await session.refresh(item)
            assert user.balance == 50_000
            assert item.status == "sold"

            second = await purchase_product(sessions, user.telegram_id, product.id, cipher)
            assert second.ok is False
            assert second.message == "out_of_stock"
        await engine.dispose()

    asyncio.run(scenario())


def test_coupon_validation_reports_the_exact_failure_reason() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        now = datetime.now(UTC)
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="San pham A",
                name_en="Product A",
                price=50_000,
            )
            other_product = Product(
                category_id=category.id,
                name_vi="San pham B",
                name_en="Product B",
                price=50_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=100_000)
            session.add_all([product, other_product, user])
            await session.flush()
            coupons = [
                DiscountCode(
                    product_id=other_product.id,
                    code="WRONGPRODUCT",
                    discount_type="fixed",
                    discount_value=5_000,
                ),
                DiscountCode(
                    product_id=product.id,
                    code="INACTIVE",
                    discount_type="fixed",
                    discount_value=5_000,
                    active=False,
                ),
                DiscountCode(
                    product_id=product.id,
                    code="FUTURE",
                    discount_type="fixed",
                    discount_value=5_000,
                    starts_at=now + timedelta(days=1),
                ),
                DiscountCode(
                    product_id=product.id,
                    code="EXPIRED",
                    discount_type="fixed",
                    discount_value=5_000,
                    expires_at=now - timedelta(days=1),
                ),
                DiscountCode(
                    product_id=product.id,
                    code="EXHAUSTED",
                    discount_type="fixed",
                    discount_value=5_000,
                    max_uses=1,
                    used_count=1,
                ),
                DiscountCode(
                    product_id=product.id,
                    code="USEDONCE",
                    discount_type="fixed",
                    discount_value=5_000,
                    max_uses=10,
                    used_count=1,
                ),
            ]
            session.add_all(coupons)
            await session.flush()
            used_coupon = coupons[-1]
            item = InventoryItem(product_id=product.id, encrypted_secret="unused")
            session.add(item)
            await session.flush()
            session.add(
                Order(
                    user_id=user.telegram_id,
                    product_id=product.id,
                    inventory_item_id=item.id,
                    amount=45_000,
                    discount_code_id=used_coupon.id,
                    discount_code=used_coupon.code,
                    status="completed",
                )
            )
            await session.commit()

        expected_errors = {
            "": "coupon_empty",
            "MISSING": "coupon_not_found",
            "WRONGPRODUCT": "coupon_wrong_product",
            "INACTIVE": "coupon_inactive",
            "FUTURE": "coupon_not_started",
            "EXPIRED": "coupon_expired",
            "EXHAUSTED": "coupon_exhausted",
            "USEDONCE": "coupon_already_used",
        }
        async with sessions() as session:
            product = await session.scalar(select(Product).where(Product.name_en == "Product A"))
            assert product is not None
            for code, expected_error in expected_errors.items():
                try:
                    await product_pricing(
                        session,
                        product,
                        coupon_code=code,
                        user_id=123456,
                        raise_coupon_error=True,
                    )
                except CouponValidationError as exc:
                    assert exc.code == expected_error
                else:
                    raise AssertionError(f"Expected coupon error {expected_error}")
        await engine.dispose()

    asyncio.run(scenario())


def test_product_coupon_reduces_each_item_and_tracks_usage() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tài khoản",
                name_en="Account",
                price=50_000,
                allow_quantity=True,
                max_quantity=10,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=100_000)
            session.add_all([product, user])
            await session.flush()
            coupon = DiscountCode(
                product_id=product.id,
                code="SAVE5K",
                discount_type="fixed",
                discount_value=5_000,
                max_uses=10,
            )
            session.add(coupon)
            session.add_all(
                [
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt(f"account{index}:password"),
                    )
                    for index in (1, 2)
                ]
            )
            await session.commit()

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            quantity=2,
            coupon_code="save5k",
        )
        assert result.ok is True
        assert result.total_amount == 90_000
        assert result.discount_amount == 10_000
        assert result.coupon_code == "SAVE5K"

        repeated = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            coupon_code="SAVE5K",
        )
        assert repeated.ok is False
        assert repeated.message == "coupon_already_used"

        async with sessions() as session:
            user = await session.get(User, user.telegram_id)
            coupon = await session.scalar(select(DiscountCode))
            orders = list(await session.scalars(select(Order).order_by(Order.id)))
            assert user is not None and user.balance == 10_000
            assert coupon is not None and coupon.used_count == 1
            assert all(order.amount == 45_000 for order in orders)
            assert all(order.discount_amount == 5_000 for order in orders)
            assert all(order.discount_code == "SAVE5K" for order in orders)
            assert all(order.cost_amount == 0 for order in orders)
        await engine.dispose()

    asyncio.run(scenario())


def test_quantity_discount_uses_highest_tier_and_stacks_with_coupon() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tai khoan so luong lon",
                name_en="Bulk account",
                price=50_000,
                allow_quantity=True,
                max_quantity=20,
            )
            user = User(telegram_id=654321, full_name="Bulk buyer", balance=1_000_000)
            session.add_all([product, user])
            await session.flush()
            coupon = DiscountCode(
                product_id=product.id,
                code="BULK5K",
                discount_type="fixed",
                discount_value=5_000,
                max_uses=5,
            )
            session.add_all(
                [
                    coupon,
                    QuantityDiscount(
                        product_id=product.id,
                        min_quantity=5,
                        discount_percent=5,
                    ),
                    QuantityDiscount(
                        product_id=product.id,
                        min_quantity=10,
                        discount_percent=10,
                    ),
                ]
            )
            session.add_all(
                [
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt(f"bulk{index}:password"),
                    )
                    for index in range(1, 11)
                ]
            )
            await session.commit()

            lower_tier = await product_pricing(session, product, quantity=6)
            assert lower_tier is not None
            assert lower_tier.quantity_discount_percent == 5
            assert lower_tier.final_unit_price == 47_500

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            quantity=10,
            coupon_code="bulk5k",
        )
        assert result.ok is True
        assert result.total_amount == 400_000
        assert result.discount_amount == 100_000
        assert result.coupon_code == "BULK5K"
        assert result.quantity_discount_percent == 10

        async with sessions() as session:
            stored_user = await session.get(User, user.telegram_id)
            stored_coupon = await session.scalar(select(DiscountCode))
            orders = list(await session.scalars(select(Order).order_by(Order.id)))
            assert stored_user is not None and stored_user.balance == 600_000
            assert stored_coupon is not None and stored_coupon.used_count == 1
            assert len(orders) == 10
            assert all(order.amount == 40_000 for order in orders)
            assert all(order.discount_amount == 10_000 for order in orders)
            assert all(order.discount_code == "BULK5K" for order in orders)
        await engine.dispose()

    asyncio.run(scenario())


def test_fixed_quantity_discount_reduces_each_account_by_configured_amount() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tài khoản giảm tiền",
                name_en="Fixed bulk discount",
                price=30_000,
                allow_quantity=True,
                max_quantity=20,
                external_stock=10,
            )
            user = User(telegram_id=7654321, full_name="Fixed buyer", balance=300_000)
            session.add_all([product, user])
            await session.flush()
            session.add(
                QuantityDiscount(
                    product_id=product.id,
                    min_quantity=10,
                    discount_type="fixed",
                    discount_amount=1_000,
                )
            )
            session.add_all(
                [
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt(f"fixed{index}:password"),
                    )
                    for index in range(1, 11)
                ]
            )
            await session.commit()

            pricing = await product_pricing(session, product, quantity=10)
            assert pricing is not None
            assert pricing.quantity_discount_type == "fixed"
            assert pricing.quantity_discount_value == 1_000
            assert pricing.final_unit_price == 29_000

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            quantity=10,
        )

        assert result.ok is True
        assert result.total_amount == 290_000
        assert result.discount_amount == 10_000
        assert result.quantity_discount_type == "fixed"
        assert result.quantity_discount_value == 1_000
        assert all(order.amount == 29_000 for order in result.orders)
        await engine.dispose()

    asyncio.run(scenario())


def test_user_activity_counts_purchase_batches_and_deposits() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tài khoản",
                name_en="Account",
                price=50_000,
                allow_quantity=True,
                max_quantity=10,
            )
            user = User(telegram_id=55555, full_name="Buyer", balance=200_000)
            session.add_all([product, user])
            await session.flush()
            session.add_all(
                [
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt(f"account{index}:password"),
                    )
                    for index in (1, 2)
                ]
            )
            await session.commit()

        purchase = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            quantity=2,
        )
        assert purchase.ok is True

        async with sessions() as session:
            deposit = Deposit(
                user_id=user.telegram_id,
                code="NAP55555ABCD",
                requested_amount=20_000,
            )
            session.add(deposit)
            await session.commit()

        await process_sepay_payment(
            sessions,
            {
                "id": 44444,
                "transferType": "in",
                "transferAmount": 20_000,
                "content": "NAP55555ABCD",
            },
        )

        async with sessions() as session:
            stats = await user_activity_stats(session, user.telegram_id)
            bundled = await order_bundle(session, user.telegram_id, purchase.orders[0].id)
            history = await recent_orders(session, user.telegram_id, limit=1)
            assert stats.purchase_count == 1
            assert stats.purchased_items == 2
            assert stats.deposit_count == 1
            assert stats.total_spent == 100_000
            assert stats.total_deposited == 20_000
            assert len(bundled) == 2
            assert len(history) == 2
            assert {order.shop_order_code for order in history} == {
                purchase.orders[0].shop_order_code
            }
        await engine.dispose()

    asyncio.run(scenario())


def test_recent_orders_returns_every_item_from_only_the_latest_batches() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tài khoản",
                name_en="Account",
                price=10_000,
                allow_quantity=True,
                max_quantity=10,
            )
            user = User(telegram_id=55666, full_name="History buyer", balance=100_000)
            session.add_all([product, user])
            await session.flush()
            session.add_all(
                [
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt(f"history-{index}"),
                    )
                    for index in range(4)
                ]
            )
            await session.commit()

        first = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            quantity=2,
        )
        second = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            quantity=2,
        )
        assert first.ok is True and second.ok is True

        async with sessions() as session:
            latest = await recent_orders(session, user.telegram_id, limit=1)
            both = await recent_orders(session, user.telegram_id, limit=2)
            assert len(latest) == 2
            assert {order.batch_code for order in latest} == {
                second.orders[0].batch_code
            }
            assert len(both) == 4
            assert {order.batch_code for order in both} == {
                first.orders[0].batch_code,
                second.orders[0].batch_code,
            }
        await engine.dispose()

    asyncio.run(scenario())


def test_sepay_payment_is_idempotent() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            user = User(telegram_id=123456, full_name="Buyer", balance=0)
            deposit = Deposit(
                user_id=user.telegram_id,
                code="NAP123456ABCD",
                requested_amount=100_000,
            )
            session.add_all([user, deposit])
            await session.commit()

        payload = {
            "id": 98765,
            "transferType": "in",
            "transferAmount": 100_000,
            "content": "NAP123456ABCD",
        }
        first = await process_sepay_payment(sessions, payload)
        second = await process_sepay_payment(sessions, payload)
        another_transfer = await process_sepay_payment(sessions, {**payload, "id": 98766})
        assert first.status == "credited"
        assert first.balance == 100_000
        assert first.deposit_code == "NAP123456ABCD"
        assert first.paid_at is not None
        assert second.status == "duplicate"
        assert another_transfer.status == "already_paid_payment"

        async with sessions() as session:
            user = await session.get(User, 123456)
            wallet_transactions = list(
                await session.scalars(select(WalletTransaction).order_by(WalletTransaction.id))
            )
            assert user is not None
            assert user.balance == 100_000
            assert len(wallet_transactions) == 1
            assert wallet_transactions[0].kind == "deposit"
            assert wallet_transactions[0].amount == 100_000
            assert wallet_transactions[0].balance_before == 0
            assert wallet_transactions[0].balance_after == 100_000
        await engine.dispose()

    asyncio.run(scenario())


def test_manual_deposit_approval_credits_once_and_late_webhook_only_matches() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            user = User(telegram_id=321654, full_name="Manual approval buyer", balance=5_000)
            deposit = Deposit(
                user_id=user.telegram_id,
                code="NAP321654MANU",
                requested_amount=20_000,
                status="pending",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            session.add_all([user, deposit])
            await session.commit()
            deposit_id = deposit.id

        approved = await approve_wallet_deposit(
            sessions,
            deposit_id,
            admin_username="admin",
        )
        duplicate_approval = await approve_wallet_deposit(
            sessions,
            deposit_id,
            admin_username="admin",
        )
        webhook = await process_sepay_payment(
            sessions,
            {
                "id": "BANK-LATE-MANUAL",
                "transferType": "in",
                "transferAmount": 20_000,
                "content": "NAP321654MANU",
            },
        )

        assert approved.status == "approved"
        assert approved.balance == 25_000
        assert duplicate_approval.status == "already_paid"
        assert webhook.status == "manual_approval_matched"
        async with sessions() as session:
            user = await session.get(User, 321654)
            deposit = await session.get(Deposit, deposit_id)
            transactions = list(
                await session.scalars(
                    select(PaymentTransaction).order_by(PaymentTransaction.id)
                )
            )
            wallet_transactions = list(await session.scalars(select(WalletTransaction)))
            adjustments = list(await session.scalars(select(BalanceAdjustment)))
            assert user is not None and user.balance == 25_000
            assert deposit is not None and deposit.status == "paid"
            assert [item.credit_status for item in transactions] == [
                "credited",
                "manual_matched",
            ]
            assert len(wallet_transactions) == 1
            assert wallet_transactions[0].amount == 20_000
            assert wallet_transactions[0].balance_before == 5_000
            assert wallet_transactions[0].balance_after == 25_000
            assert len(adjustments) == 1
            assert adjustments[0].admin_username == "admin"
        await engine.dispose()

    asyncio.run(scenario())


def test_manual_deposit_cancellation_rejects_pending_requests() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            user = User(telegram_id=321655, full_name="Cancelled deposit buyer", balance=5_000)
            deposit = Deposit(
                user_id=user.telegram_id,
                code="NAP321655CANC",
                requested_amount=20_000,
                status="pending",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            session.add_all([user, deposit])
            await session.commit()
            deposit_id = deposit.id

        cancelled = await cancel_wallet_deposit(sessions, deposit_id)
        assert cancelled.status == "invalid_status"
        async with sessions() as session:
            user = await session.get(User, 321655)
            deposit = await session.get(Deposit, deposit_id)
            assert user is not None and user.balance == 5_000
            assert deposit is not None and deposit.status == "pending"
            assert deposit.failure_reason is None
            assert deposit.failed_at is None
            assert await session.scalar(select(PaymentTransaction.id)) is None
            assert await session.scalar(select(WalletTransaction.id)) is None
            assert await session.scalar(select(BalanceAdjustment.id)) is None

        webhook = await process_sepay_payment(
            sessions,
            {
                "id": "BANK-LATE-CANCELLED",
                "transferType": "in",
                "transferAmount": 20_000,
                "content": "NAP321655CANC",
            },
        )
        assert webhook.status == "credited"
        async with sessions() as session:
            user = await session.get(User, 321655)
            transaction = await session.scalar(select(PaymentTransaction))
            assert user is not None and user.balance == 25_000
            assert transaction is not None
            assert transaction.credit_status == "credited"
            wallet_transaction = await session.scalar(select(WalletTransaction))
            assert wallet_transaction is not None
            assert wallet_transaction.amount == 20_000
            assert await session.scalar(select(BalanceAdjustment.id)) is None
        await engine.dispose()

    asyncio.run(scenario())


def test_manual_deposit_cancellation_allows_expired_requests() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            user = User(telegram_id=321656, full_name="Expired deposit buyer", balance=5_000)
            deposit = Deposit(
                user_id=user.telegram_id,
                code="NAP321656EXPD",
                requested_amount=20_000,
                status="failed",
                failure_reason="expired",
                failed_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) - timedelta(minutes=1),
            )
            session.add_all([user, deposit])
            await session.commit()
            deposit_id = deposit.id

        cancelled = await cancel_wallet_deposit(sessions, deposit_id)
        assert cancelled.status == "cancelled"
        async with sessions() as session:
            user = await session.get(User, 321656)
            deposit = await session.get(Deposit, deposit_id)
            assert user is not None and user.balance == 5_000
            assert deposit is not None
            assert deposit.status == "failed"
            assert deposit.failure_reason == "admin_cancelled"
            assert await session.scalar(select(PaymentTransaction.id)) is None
            assert await session.scalar(select(WalletTransaction.id)) is None
        await engine.dispose()

    asyncio.run(scenario())


def test_manual_direct_purchase_approves_delivery_or_wallet_fallback() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="QR", name_en="QR")
            user = User(telegram_id=321657, full_name="QR buyer", balance=1_000)
            session.add_all([category, user])
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="QR account",
                name_en="QR account",
                price=20_000,
            )
            session.add(product)
            await session.flush()
            item = InventoryItem(
                product_id=product.id,
                encrypted_secret=cipher.encrypt("qr-account|password"),
            )
            delivered = Deposit(
                user_id=user.telegram_id,
                code="NAP321657DELI",
                requested_amount=20_000,
                payment_kind="direct_purchase",
                product_id=product.id,
                quantity=1,
                status="pending",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            fallback = Deposit(
                user_id=user.telegram_id,
                code="NAP321657FALL",
                requested_amount=30_000,
                payment_kind="direct_purchase",
                product_id=product.id,
                quantity=2,
            )
            session.add_all([item, delivered, fallback])
            await session.commit()
            delivered_id = delivered.id
            fallback_id = fallback.id

        delivered_result = await approve_direct_purchase_deposit(
            sessions,
            delivered_id,
            cipher=cipher,
        )
        assert delivered_result.status == "direct_purchase_completed"
        assert delivered_result.order_ids
        assert [cipher.decrypt(value) for value in delivered_result.encrypted_secrets] == [
            "qr-account|password"
        ]
        late_webhook = await process_sepay_payment(
            sessions,
            {
                "id": "BANK-AFTER-MANUAL-DIRECT",
                "transferType": "in",
                "transferAmount": 20_000,
                "content": "NAP321657DELI",
            },
            cipher=cipher,
        )
        assert late_webhook.status == "manual_approval_matched"
        duplicate = await approve_direct_purchase_deposit(
            sessions,
            delivered_id,
            cipher=cipher,
        )
        assert duplicate.status == "already_paid_payment"

        fallback_result = await approve_direct_purchase_deposit(
            sessions,
            fallback_id,
            cipher=cipher,
        )
        assert fallback_result.status == "direct_purchase_fallback"
        assert fallback_result.balance == 31_000

        async with sessions() as session:
            delivered = await session.get(Deposit, delivered_id)
            fallback = await session.get(Deposit, fallback_id)
            user = await session.get(User, 321657)
            assert delivered is not None and delivered.status == "paid"
            assert fallback is not None and fallback.status == "paid"
            assert user is not None and user.balance == 31_000
            wallet_transaction = await session.scalar(
                select(WalletTransaction).where(
                    WalletTransaction.event_key == f"deposit:{fallback_id}"
                )
            )
            assert wallet_transaction is not None
            assert wallet_transaction.kind == "direct_purchase_fallback"
        await engine.dispose()

    asyncio.run(scenario())


def test_manual_direct_purchase_cancellation_blocks_late_webhook() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="QR", name_en="QR")
            user = User(telegram_id=321658, full_name="QR cancel buyer")
            session.add_all([category, user])
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="QR account",
                name_en="QR account",
                price=20_000,
            )
            session.add(product)
            await session.flush()
            deposit = Deposit(
                user_id=user.telegram_id,
                code="NAP321658CANC",
                requested_amount=20_000,
                payment_kind="direct_purchase",
                product_id=product.id,
                quantity=1,
                status="failed",
                failure_reason="expired",
                failed_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) - timedelta(minutes=1),
            )
            session.add(deposit)
            await session.commit()
            deposit_id = deposit.id

        cancelled = await cancel_direct_purchase_deposit(sessions, deposit_id)
        assert cancelled.status == "cancelled"
        late = await process_sepay_payment(
            sessions,
            {
                "id": "BANK-LATE-DIRECT-CANCEL",
                "transferType": "in",
                "transferAmount": 20_000,
                "content": "NAP321658CANC",
            },
        )
        assert late.status == "failed_request_payment"
        async with sessions() as session:
            user = await session.get(User, 321658)
            deposit = await session.get(Deposit, deposit_id)
            assert user is not None and user.balance == 0
            assert deposit is not None and deposit.failure_reason == "admin_cancelled"
        await engine.dispose()

    asyncio.run(scenario())


def test_manual_direct_purchase_cancellation_rejects_pending_request() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="QR", name_en="QR")
            user = User(telegram_id=321659, full_name="QR pending buyer")
            session.add_all([category, user])
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="QR account",
                name_en="QR account",
                price=20_000,
            )
            session.add(product)
            await session.flush()
            deposit = Deposit(
                user_id=user.telegram_id,
                code="NAP321659PEND",
                requested_amount=20_000,
                payment_kind="direct_purchase",
                product_id=product.id,
                quantity=1,
                status="pending",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            session.add(deposit)
            await session.commit()
            deposit_id = deposit.id

        result = await cancel_direct_purchase_deposit(sessions, deposit_id)
        assert result.status == "invalid_status"
        async with sessions() as session:
            deposit = await session.get(Deposit, deposit_id)
            assert deposit is not None
            assert deposit.status == "pending"
            assert deposit.failure_reason is None
        await engine.dispose()

    asyncio.run(scenario())


def test_direct_purchase_payment_delivers_without_using_wallet() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tài khoản",
                name_en="Account",
                price=50_000,
                allow_quantity=True,
                max_quantity=10,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=10_000)
            session.add_all([product, user])
            await session.flush()
            items = [
                InventoryItem(
                    product_id=product.id,
                    encrypted_secret=cipher.encrypt(f"account{index}:password"),
                )
                for index in (1, 2)
            ]
            deposit = Deposit(
                user_id=user.telegram_id,
                code="NAP123456ABCD",
                requested_amount=100_000,
                payment_kind="direct_purchase",
                product_id=product.id,
                quantity=2,
            )
            session.add_all([*items, deposit])
            await session.commit()

        payload = {
            "id": 22222,
            "transferType": "in",
            "transferAmount": 100_000,
            "content": "NAP123456ABCD",
        }
        result = await process_sepay_payment(sessions, payload)
        duplicate = await process_sepay_payment(sessions, payload)
        assert result.status == "direct_purchase_completed"
        assert len(result.order_ids) == 2
        assert result.shop_order_code is not None
        assert result.shop_order_code.startswith("B")
        assert [cipher.decrypt(value) for value in result.encrypted_secrets] == [
            "account1:password",
            "account2:password",
        ]
        assert duplicate.status == "duplicate"

        async with sessions() as session:
            user = await session.get(User, 123456)
            stock_items = list(await session.scalars(select(InventoryItem)))
            order_count = await session.scalar(select(func.count(Order.id)))
            wallet_count = int(
                await session.scalar(select(func.count(WalletTransaction.id))) or 0
            )
            assert user is not None and user.balance == 10_000
            assert all(item.status == "sold" for item in stock_items)
            assert order_count == 2
            assert wallet_count == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_direct_purchase_keeps_seller_margin_snapshot_and_exact_item_costs() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="Seller", name_en="Seller")
            seller = User(telegram_id=123457, full_name="Seller", balance=10_000)
            session.add_all([category, seller])
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tài khoản seller",
                name_en="Seller account",
                price=40_000,
                allow_quantity=True,
                max_quantity=10,
            )
            session.add(product)
            await session.flush()
            rule = SellerPrice(
                user_id=seller.telegram_id,
                product_id=product.id,
                profit_per_unit=5_000,
            )
            session.add(rule)
            session.add_all(
                [
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt("seller-qr-1|password"),
                        cost_amount=30_000,
                    ),
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt("seller-qr-2|password"),
                        cost_amount=31_000,
                    ),
                ]
            )
            await session.flush()
            deposit = await create_deposit(
                session,
                seller.telegram_id,
                71_000,
                payment_kind="direct_purchase",
                product_id=product.id,
                quantity=2,
                seller_price_id=rule.id,
                seller_profit_per_unit=5_000,
            )

        result = await process_sepay_payment(
            sessions,
            {
                "id": 22223,
                "transferType": "in",
                "transferAmount": 71_000,
                "content": deposit.code,
            },
        )
        assert result.status == "direct_purchase_completed"
        async with sessions() as session:
            stored_deposit = await session.get(Deposit, deposit.id)
            orders = list(await session.scalars(select(Order).order_by(Order.id)))
            seller = await session.get(User, 123457)
            assert stored_deposit is not None
            assert stored_deposit.seller_price_id == rule.id
            assert stored_deposit.seller_profit_per_unit == 5_000
            assert [order.amount for order in orders] == [35_000, 36_000]
            assert all(order.seller_price_id == rule.id for order in orders)
            assert seller is not None and seller.balance == 10_000
        await engine.dispose()

    asyncio.run(scenario())


def test_direct_purchase_honors_reserved_coupon_price() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tài khoản",
                name_en="Account",
                price=50_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=0)
            session.add_all([product, user])
            await session.flush()
            coupon = DiscountCode(
                product_id=product.id,
                code="QR5K",
                discount_type="fixed",
                discount_value=5_000,
            )
            session.add(coupon)
            await session.flush()
            session.add_all(
                [
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt("account:password"),
                    ),
                    Deposit(
                        user_id=user.telegram_id,
                        code="NAP123456ABCD",
                        requested_amount=45_000,
                        payment_kind="direct_purchase",
                        product_id=product.id,
                        discount_amount=5_000,
                        discount_code_id=coupon.id,
                        discount_code=coupon.code,
                    ),
                ]
            )
            await session.commit()

        result = await process_sepay_payment(
            sessions,
            {
                "id": 22333,
                "transferType": "in",
                "transferAmount": 45_000,
                "content": "NAP123456ABCD",
            },
        )
        assert result.status == "direct_purchase_completed"

        async with sessions() as session:
            order = await session.scalar(select(Order))
            coupon = await session.scalar(select(DiscountCode))
            assert order is not None and order.amount == 45_000
            assert order.discount_amount == 5_000
            assert order.discount_code == "QR5K"
            assert coupon is not None and coupon.used_count == 1
        await engine.dispose()

    asyncio.run(scenario())


def test_direct_purchase_does_not_reuse_a_coupon_for_the_same_user() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tai khoan",
                name_en="Account",
                price=50_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=0)
            session.add_all([product, user])
            await session.flush()
            coupon = DiscountCode(
                product_id=product.id,
                code="ONCEONLY",
                discount_type="fixed",
                discount_value=5_000,
                max_uses=10,
                used_count=1,
            )
            session.add(coupon)
            await session.flush()
            sold_item = InventoryItem(
                product_id=product.id,
                encrypted_secret="sold",
                status="sold",
            )
            available_item = InventoryItem(
                product_id=product.id,
                encrypted_secret="available",
            )
            session.add_all([sold_item, available_item])
            await session.flush()
            session.add_all(
                [
                    Order(
                        user_id=user.telegram_id,
                        product_id=product.id,
                        inventory_item_id=sold_item.id,
                        amount=45_000,
                        discount_code_id=coupon.id,
                        discount_code=coupon.code,
                        status="completed",
                    ),
                    Deposit(
                        user_id=user.telegram_id,
                        code="NAP123456CDEF",
                        requested_amount=45_000,
                        payment_kind="direct_purchase",
                        product_id=product.id,
                        discount_amount=5_000,
                        discount_code_id=coupon.id,
                        discount_code=coupon.code,
                    ),
                ]
            )
            await session.commit()
            available_item_id = available_item.id

        result = await process_sepay_payment(
            sessions,
            {
                "id": 22334,
                "transferType": "in",
                "transferAmount": 45_000,
                "content": "NAP123456CDEF",
            },
        )
        assert result.status == "direct_purchase_fallback"

        async with sessions() as session:
            stored_user = await session.get(User, 123456)
            stored_coupon = await session.scalar(select(DiscountCode))
            available_item = await session.get(InventoryItem, available_item_id)
            order_count = int(await session.scalar(select(func.count(Order.id))) or 0)
            assert stored_user is not None and stored_user.balance == 45_000
            assert stored_coupon is not None and stored_coupon.used_count == 1
            assert available_item is not None and available_item.status == "available"
            assert order_count == 1
        await engine.dispose()

    asyncio.run(scenario())


def test_direct_purchase_falls_back_to_wallet_when_stock_is_gone() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tài khoản",
                name_en="Account",
                price=50_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=0)
            session.add_all([product, user])
            await session.flush()
            session.add(
                Deposit(
                    user_id=user.telegram_id,
                    code="NAP123456ABCD",
                    requested_amount=50_000,
                    payment_kind="direct_purchase",
                    product_id=product.id,
                )
            )
            await session.commit()

        result = await process_sepay_payment(
            sessions,
            {
                "id": 33333,
                "transferType": "in",
                "transferAmount": 50_000,
                "content": "NAP123456ABCD",
            },
        )
        assert result.status == "direct_purchase_fallback"

        async with sessions() as session:
            user = await session.get(User, 123456)
            wallet_transaction = await session.scalar(select(WalletTransaction))
            assert user is not None and user.balance == 50_000
            assert wallet_transaction is not None
            assert wallet_transaction.kind == "direct_purchase_fallback"
            assert wallet_transaction.amount == 50_000
        await engine.dispose()

    asyncio.run(scenario())


def test_manual_stock_zero_preserves_inventory_and_blocks_all_purchase_sources() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = FakeSupplier(balance=100_000, stock=20)
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            local_product = Product(
                category_id=category.id,
                name_vi="Kho local tạm dừng",
                name_en="Paused local",
                price=10_000,
                force_out_of_stock=True,
            )
            api_product = Product(
                category_id=category.id,
                name_vi="API tạm dừng",
                name_en="Paused API",
                price=20_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-PAUSED",
                external_stock=20,
                force_out_of_stock=True,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=100_000)
            session.add_all([local_product, api_product, user])
            await session.flush()
            item = InventoryItem(
                product_id=local_product.id,
                encrypted_secret=cipher.encrypt("preserved:account"),
            )
            session.add(item)
            await session.commit()

        async with sessions() as session:
            assert await available_stock(session, local_product.id) == 0
            assert (
                await available_stock(
                    session,
                    api_product.id,
                    supplier,  # type: ignore[arg-type]
                    refresh_external=True,
                )
                == 0
            )

        local_result = await purchase_product(
            sessions,
            user.telegram_id,
            local_product.id,
            cipher,
        )
        api_result = await purchase_product(
            sessions,
            user.telegram_id,
            api_product.id,
            cipher,
            supplier_client=supplier,  # type: ignore[arg-type]
        )

        assert local_result.message == "out_of_stock"
        assert api_result.message == "out_of_stock"
        assert supplier.buy_calls == 0
        async with sessions() as session:
            stored_item = await session.get(InventoryItem, item.id)
            stored_api_product = await session.get(Product, api_product.id)
            stored_user = await session.get(User, user.telegram_id)
            assert stored_item is not None and stored_item.status == "available"
            assert stored_api_product is not None and stored_api_product.external_stock == 20
            assert stored_user is not None and stored_user.balance == 100_000
        await engine.dispose()

    asyncio.run(scenario())


def test_direct_purchase_manual_stock_zero_falls_back_without_consuming_inventory() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tài khoản tạm dừng",
                name_en="Paused account",
                price=50_000,
                force_out_of_stock=True,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=0)
            session.add_all([product, user])
            await session.flush()
            item = InventoryItem(
                product_id=product.id,
                encrypted_secret="preserved-secret",
            )
            session.add_all(
                [
                    item,
                    Deposit(
                        user_id=user.telegram_id,
                        code="NAP123456EFGH",
                        requested_amount=50_000,
                        payment_kind="direct_purchase",
                        product_id=product.id,
                    ),
                ]
            )
            await session.commit()

        result = await process_sepay_payment(
            sessions,
            {
                "id": 33334,
                "transferType": "in",
                "transferAmount": 50_000,
                "content": "NAP123456EFGH",
            },
        )

        assert result.status == "direct_purchase_fallback"
        async with sessions() as session:
            stored_user = await session.get(User, user.telegram_id)
            stored_item = await session.get(InventoryItem, item.id)
            assert stored_user is not None and stored_user.balance == 50_000
            assert stored_item is not None and stored_item.status == "available"
        await engine.dispose()

    asyncio.run(scenario())


def test_gpt_plus_combines_supplier_stock_and_prices_each_source_tier() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        sumi = RoutedSupplier(
            "sumistore",
            unit_price=30_000,
            stock=5,
            balance=150_000,
        )
        lehai = RoutedSupplier(
            "lehai",
            unit_price=25_000,
            stock=10,
            balance=250_000,
        )
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus",
                name_en="GPT Plus",
                price=35_000,
                allow_quantity=True,
                max_quantity=100,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_price=30_000,
                supplier_markup=5_000,
                supplier_synced_at=datetime.now(UTC),
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=335_000)
            session.add_all([product, user])
            await session.commit()
            product_id = product.id

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product_id,
            cipher,
            quantity=11,
            supplier_client=sumi,  # type: ignore[arg-type]
            lehai_client=lehai,  # type: ignore[arg-type]
        )

        assert result.ok is True
        assert result.total_amount == 335_000
        assert sumi.buy_quantities == [1]
        assert lehai.buy_quantities == [10]
        assert len({order.batch_code for order in result.orders}) == 1
        assert [order.amount for order in result.orders].count(30_000) == 10
        assert [order.amount for order in result.orders].count(35_000) == 1
        assert [order.cost_amount for order in result.orders].count(25_000) == 10
        assert [order.cost_amount for order in result.orders].count(30_000) == 1
        assert [order.supplier_provider for order in result.orders].count("lehai") == 10
        assert [order.supplier_provider for order in result.orders].count("sumistore") == 1
        assert all(order.product_name_vi == "GPT Plus" for order in result.orders)
        assert all(order.product_name_en == "GPT Plus" for order in result.orders)
        async with sessions() as session:
            product = await session.get(Product, product_id)
            assert product is not None
            assert product.price == 30_000
            assert product.supplier_available_stock == 15
            assert product.external_stock == 4
            price_alert = await session.scalar(select(ProductPriceAlert))
            assert price_alert is not None
            assert price_alert.supplier_price_before == 30_000
            assert price_alert.supplier_price_after == 25_000
            assert price_alert.sale_price_before == 35_000
            assert price_alert.sale_price_after == 30_000
        await engine.dispose()

    asyncio.run(scenario())


def test_gpt_plus_equal_supplier_prices_prefer_sumi() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        sumi = RoutedSupplier(
            "sumistore",
            unit_price=25_000,
            stock=2,
            balance=50_000,
        )
        lehai = RoutedSupplier(
            "lehai",
            unit_price=25_000,
            stock=2,
            balance=50_000,
        )
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus",
                name_en="GPT Plus",
                price=30_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_price=25_000,
                supplier_markup=5_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=30_000)
            session.add_all([product, user])
            await session.commit()
            product_id = product.id

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product_id,
            cipher,
            supplier_client=sumi,  # type: ignore[arg-type]
            lehai_client=lehai,  # type: ignore[arg-type]
        )

        assert result.ok is True
        assert sumi.buy_quantities == [1]
        assert lehai.buy_quantities == []
        assert result.orders[0].supplier_provider == "sumistore"
        await engine.dispose()

    asyncio.run(scenario())


def test_gpt_plus_disabled_sumi_uses_only_lehai() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        sumi = RoutedSupplier(
            "sumistore",
            unit_price=20_000,
            stock=10,
            balance=200_000,
        )
        lehai = RoutedSupplier(
            "lehai",
            unit_price=25_000,
            stock=10,
            balance=250_000,
        )
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus",
                name_en="GPT Plus",
                price=30_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_price=25_000,
                supplier_markup=5_000,
                sumistore_api_enabled=False,
                lehai_api_enabled=True,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=30_000)
            session.add_all([product, user])
            await session.commit()
            product_id = product.id

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product_id,
            cipher,
            supplier_client=sumi,  # type: ignore[arg-type]
            lehai_client=lehai,  # type: ignore[arg-type]
        )

        assert result.ok is True
        assert sumi.fetch_product_ids == []
        assert sumi.buy_quantities == []
        assert lehai.fetch_product_ids == ["gpt_bh48_1m"]
        assert lehai.buy_quantities == [1]
        assert result.orders[0].cost_amount == 25_000
        assert result.orders[0].supplier_provider == "lehai"
        await engine.dispose()

    asyncio.run(scenario())


def test_gpt_plus_with_both_apis_disabled_sells_only_local_inventory() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        sumi = RoutedSupplier(
            "sumistore",
            unit_price=20_000,
            stock=10,
            balance=200_000,
        )
        lehai = RoutedSupplier(
            "lehai",
            unit_price=25_000,
            stock=10,
            balance=250_000,
        )
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus local only",
                name_en="GPT Plus local only",
                price=30_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_price=25_000,
                supplier_markup=5_000,
                sumistore_api_enabled=False,
                lehai_api_enabled=False,
                external_stock=99,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=60_000)
            session.add_all([product, user])
            await session.flush()
            session.add(
                InventoryItem(
                    product_id=product.id,
                    encrypted_secret=cipher.encrypt("local-account|password"),
                    cost_amount=22_000,
                )
            )
            await session.commit()
            product_id = product.id

        first = await purchase_product(
            sessions,
            user.telegram_id,
            product_id,
            cipher,
            supplier_client=sumi,  # type: ignore[arg-type]
            lehai_client=lehai,  # type: ignore[arg-type]
        )
        second = await purchase_product(
            sessions,
            user.telegram_id,
            product_id,
            cipher,
            supplier_client=sumi,  # type: ignore[arg-type]
            lehai_client=lehai,  # type: ignore[arg-type]
        )

        assert first.ok is True
        assert first.secrets == ["local-account|password"]
        assert second.ok is False and second.message == "out_of_stock"
        assert sumi.fetch_product_ids == []
        assert sumi.buy_quantities == []
        assert lehai.fetch_product_ids == []
        assert lehai.buy_quantities == []
        async with sessions() as session:
            product = await session.get(Product, product_id)
            assert product is not None and product.external_stock == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_gpt_plus_qr_credits_wallet_when_cheap_stock_is_gone() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        sumi = RoutedSupplier(
            "sumistore",
            unit_price=30_000,
            stock=5,
            balance=150_000,
        )
        lehai = RoutedSupplier(
            "lehai",
            unit_price=25_000,
            stock=0,
            balance=250_000,
        )
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus",
                name_en="GPT Plus",
                price=30_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_price=25_000,
                supplier_markup=5_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=0)
            session.add_all([product, user])
            await session.flush()
            session.add(
                Deposit(
                    user_id=user.telegram_id,
                    code="NAP123456ABCD",
                    requested_amount=30_000,
                    payment_kind="direct_purchase",
                    product_id=product.id,
                    quantity=1,
                )
            )
            await session.commit()

        result = await process_sepay_payment(
            sessions,
            {
                "id": 99123,
                "transferType": "in",
                "transferAmount": 30_000,
                "content": "NAP123456ABCD",
            },
            cipher=cipher,
            supplier_client=sumi,  # type: ignore[arg-type]
            lehai_client=lehai,  # type: ignore[arg-type]
        )

        assert result.status == "direct_purchase_fallback"
        assert sumi.buy_quantities == []
        assert lehai.buy_quantities == []
        async with sessions() as session:
            user = await session.get(User, 123456)
            assert user is not None and user.balance == 30_000
            assert await session.scalar(select(func.count(Order.id))) == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_gpt_plus_qr_credits_wallet_when_only_part_of_cheap_stock_remains() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        sumi = RoutedSupplier(
            "sumistore",
            unit_price=30_000,
            stock=2,
            balance=60_000,
        )
        lehai = RoutedSupplier(
            "lehai",
            unit_price=25_000,
            stock=3,
            balance=75_000,
        )
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus",
                name_en="GPT Plus",
                price=30_000,
                allow_quantity=True,
                max_quantity=100,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_price=25_000,
                supplier_markup=5_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=0)
            session.add_all([product, user])
            await session.flush()
            session.add(
                Deposit(
                    user_id=user.telegram_id,
                    code="NAP123456PART",
                    requested_amount=150_000,
                    payment_kind="direct_purchase",
                    product_id=product.id,
                    quantity=5,
                )
            )
            await session.commit()

        result = await process_sepay_payment(
            sessions,
            {
                "id": 99125,
                "transferType": "in",
                "transferAmount": 150_000,
                "content": "NAP123456PART",
            },
            cipher=cipher,
            supplier_client=sumi,  # type: ignore[arg-type]
            lehai_client=lehai,  # type: ignore[arg-type]
        )

        assert result.status == "direct_purchase_fallback"
        assert sumi.buy_quantities == []
        assert lehai.buy_quantities == []
        assert sumi.stock == 2
        assert lehai.stock == 3
        async with sessions() as session:
            user = await session.get(User, 123456)
            assert user is not None and user.balance == 150_000
            assert await session.scalar(select(func.count(Order.id))) == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_gpt_plus_qr_uses_other_supplier_when_current_price_is_unchanged() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        sumi = RoutedSupplier(
            "sumistore",
            unit_price=25_000,
            stock=2,
            balance=50_000,
        )
        lehai = RoutedSupplier(
            "lehai",
            unit_price=25_000,
            stock=0,
            balance=50_000,
        )
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus",
                name_en="GPT Plus",
                price=30_000,
                allow_quantity=True,
                max_quantity=100,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_price=25_000,
                supplier_markup=5_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=0)
            session.add_all([product, user])
            await session.flush()
            session.add(
                Deposit(
                    user_id=user.telegram_id,
                    code="NAP123456SAME",
                    requested_amount=60_000,
                    payment_kind="direct_purchase",
                    product_id=product.id,
                    quantity=2,
                )
            )
            await session.commit()

        result = await process_sepay_payment(
            sessions,
            {
                "id": 99126,
                "transferType": "in",
                "transferAmount": 60_000,
                "content": "NAP123456SAME",
            },
            cipher=cipher,
            supplier_client=sumi,  # type: ignore[arg-type]
            lehai_client=lehai,  # type: ignore[arg-type]
        )

        assert result.status == "direct_purchase_completed"
        assert sumi.buy_quantities == [2]
        assert lehai.buy_quantities == []
        async with sessions() as session:
            user = await session.get(User, 123456)
            orders = list(await session.scalars(select(Order).order_by(Order.id)))
            assert user is not None and user.balance == 0
            assert len(orders) == 2
            assert sum(order.amount for order in orders) == 60_000
            assert all(order.product_name_vi == "GPT Plus" for order in orders)
            assert all(order.product_name_en == "GPT Plus" for order in orders)
        await engine.dispose()

    asyncio.run(scenario())


def test_gpt_plus_qr_delivers_one_order_from_both_suppliers() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        sumi = RoutedSupplier(
            "sumistore",
            unit_price=30_000,
            stock=5,
            balance=150_000,
        )
        lehai = RoutedSupplier(
            "lehai",
            unit_price=25_000,
            stock=10,
            balance=250_000,
        )
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus",
                name_en="GPT Plus",
                price=30_000,
                allow_quantity=True,
                max_quantity=100,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_price=25_000,
                supplier_markup=5_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=0)
            session.add_all([product, user])
            await session.flush()
            session.add(
                Deposit(
                    user_id=user.telegram_id,
                    code="NAP123456ABCD",
                    requested_amount=335_000,
                    payment_kind="direct_purchase",
                    product_id=product.id,
                    quantity=11,
                )
            )
            await session.commit()

        result = await process_sepay_payment(
            sessions,
            {
                "id": 99124,
                "transferType": "in",
                "transferAmount": 335_000,
                "content": "NAP123456ABCD",
            },
            cipher=cipher,
            supplier_client=sumi,  # type: ignore[arg-type]
            lehai_client=lehai,  # type: ignore[arg-type]
        )

        assert result.status == "direct_purchase_completed"
        assert result.quantity == 11
        assert sumi.buy_quantities == [1]
        assert lehai.buy_quantities == [10]
        async with sessions() as session:
            orders = list(await session.scalars(select(Order).order_by(Order.id)))
            user = await session.get(User, 123456)
            assert len(orders) == 11
            assert len({order.batch_code for order in orders}) == 1
            assert [order.amount for order in orders].count(30_000) == 10
            assert [order.amount for order in orders].count(35_000) == 1
            assert user is not None and user.balance == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_concurrent_gpt_plus_qr_payments_never_oversell_cheap_stock() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        sumi = RoutedSupplier(
            "sumistore",
            unit_price=30_000,
            stock=5,
            balance=150_000,
        )
        lehai = RoutedSupplier(
            "lehai",
            unit_price=25_000,
            stock=10,
            balance=250_000,
        )
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus",
                name_en="GPT Plus",
                price=30_000,
                allow_quantity=True,
                max_quantity=100,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_price=25_000,
                supplier_markup=5_000,
            )
            session.add(product)
            await session.flush()
            for index in range(11):
                user_id = 20_000 + index
                session.add(User(telegram_id=user_id, full_name=f"Buyer {index}"))
                session.add(
                    Deposit(
                        user_id=user_id,
                        code=f"NAP{user_id}A{index:03d}",
                        requested_amount=30_000,
                        payment_kind="direct_purchase",
                        product_id=product.id,
                        quantity=1,
                    )
                )
            await session.commit()

        results = await asyncio.gather(
            *(
                process_sepay_payment(
                    sessions,
                    {
                        "id": 100_000 + index,
                        "transferType": "in",
                        "transferAmount": 30_000,
                        "content": f"NAP{20_000 + index}A{index:03d}",
                    },
                    cipher=cipher,
                    supplier_client=sumi,  # type: ignore[arg-type]
                    lehai_client=lehai,  # type: ignore[arg-type]
                )
                for index in range(11)
            )
        )

        assert [result.status for result in results].count(
            "direct_purchase_completed"
        ) == 10
        assert [result.status for result in results].count(
            "direct_purchase_fallback"
        ) == 1
        assert sum(lehai.buy_quantities) == 10
        assert sumi.buy_quantities == []
        async with sessions() as session:
            assert int(await session.scalar(select(func.count(Order.id))) or 0) == 10
            wallet_balances = list(
                await session.scalars(select(User.balance).order_by(User.telegram_id))
            )
            assert wallet_balances.count(30_000) == 1
            assert wallet_balances.count(0) == 10
        await engine.dispose()

    asyncio.run(scenario())


def test_concurrent_gpt_plus_qr_payments_with_mixed_quantities_are_atomic() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        sumi = RoutedSupplier(
            "sumistore",
            unit_price=30_000,
            stock=5,
            balance=150_000,
        )
        lehai = RoutedSupplier(
            "lehai",
            unit_price=25_000,
            stock=10,
            balance=250_000,
        )
        quantities = (2, 3, 5, 2)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus",
                name_en="GPT Plus",
                price=30_000,
                allow_quantity=True,
                max_quantity=100,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_price=25_000,
                supplier_markup=5_000,
            )
            session.add(product)
            await session.flush()
            for index, quantity in enumerate(quantities):
                user_id = 30_000 + index
                session.add(User(telegram_id=user_id, full_name=f"Buyer {index}"))
                session.add(
                    Deposit(
                        user_id=user_id,
                        code=f"NAP{user_id}Q{quantity:03d}",
                        requested_amount=quantity * 30_000,
                        payment_kind="direct_purchase",
                        product_id=product.id,
                        quantity=quantity,
                    )
                )
            await session.commit()

        results = await asyncio.gather(
            *(
                process_sepay_payment(
                    sessions,
                    {
                        "id": 110_000 + index,
                        "transferType": "in",
                        "transferAmount": quantity * 30_000,
                        "content": f"NAP{30_000 + index}Q{quantity:03d}",
                    },
                    cipher=cipher,
                    supplier_client=sumi,  # type: ignore[arg-type]
                    lehai_client=lehai,  # type: ignore[arg-type]
                )
                for index, quantity in enumerate(quantities)
            )
        )

        assert [result.status for result in results] == [
            "direct_purchase_completed",
            "direct_purchase_completed",
            "direct_purchase_completed",
            "direct_purchase_fallback",
        ]
        assert sum(lehai.buy_quantities) == 10
        assert sumi.buy_quantities == []
        async with sessions() as session:
            orders = list(await session.scalars(select(Order).order_by(Order.id)))
            users = {
                user.telegram_id: user
                for user in await session.scalars(select(User).order_by(User.telegram_id))
            }
            order_counts: dict[int, int] = {}
            for order in orders:
                order_counts[order.user_id] = order_counts.get(order.user_id, 0) + 1
            assert len(orders) == 10
            assert order_counts == {30_000: 2, 30_001: 3, 30_002: 5}
            assert users[30_000].balance == 0
            assert users[30_001].balance == 0
            assert users[30_002].balance == 0
            assert users[30_003].balance == 60_000
        await engine.dispose()

    asyncio.run(scenario())


def test_external_purchase_uses_dynamic_price_and_delivers_accounts() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = FakeSupplier(balance=30_000, stock=100)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="ChatGPT Plus",
                name_en="ChatGPT Plus",
                price=99_000,
                allow_quantity=True,
                max_quantity=10,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_markup=5_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=50_000)
            session.add_all([product, user])
            await session.commit()

        result = await purchase_product(
            sessions,
            123456,
            product.id,
            cipher,
            2,
            supplier,  # type: ignore[arg-type]
        )
        assert result.ok is True
        assert result.total_amount == 40_000
        assert result.secrets == ["chatgpt1:password", "chatgpt2:password"]
        assert supplier.buy_calls == 1

        async with sessions() as session:
            user = await session.get(User, 123456)
            product = await session.get(Product, product.id)
            orders = list(await session.scalars(select(Order).order_by(Order.id)))
            wallet_transaction = await session.scalar(select(WalletTransaction))
            assert user is not None and user.balance == 10_000
            assert product is not None and product.price == 20_000
            assert product.external_stock == 0
            assert all(order.amount == 20_000 for order in orders)
            assert all(order.cost_amount == 15_000 for order in orders)
            assert all(order.supplier_order_code == "API-TELE-TEST123" for order in orders)
            assert all(order.supplier_provider == "sumistore" for order in orders)
            assert len({order.batch_code for order in orders}) == 1
            assert wallet_transaction is not None
            assert wallet_transaction.kind == "product_purchase"
            assert wallet_transaction.amount == -40_000
            assert wallet_transaction.balance_before == 50_000
            assert wallet_transaction.balance_after == 10_000
            assert wallet_transaction.reference_id == orders[0].batch_code
        await engine.dispose()

    asyncio.run(scenario())


def test_external_purchase_recovers_supplier_order_after_timeout() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = TimeoutRecoveringSupplier(balance=30_000, stock=100)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="ChatGPT Plus",
                name_en="ChatGPT Plus",
                price=20_000,
                allow_quantity=True,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_markup=5_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=50_000)
            session.add_all([product, user])
            await session.commit()

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            2,
            supplier,  # type: ignore[arg-type]
        )

        assert result.ok is True
        assert result.secrets == ["recovered1:password", "recovered2:password"]
        async with sessions() as session:
            orders = list(await session.scalars(select(Order).order_by(Order.id)))
            assert len(orders) == 2
            assert all(
                order.supplier_order_code == "API-TELE-RECOVERED" for order in orders
            )
        await engine.dispose()

    asyncio.run(scenario())


def test_external_purchase_queues_late_recovery_without_charging_user() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = PendingRecoverySupplier(balance=30_000, stock=100)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="ChatGPT Plus",
                name_en="ChatGPT Plus",
                price=20_000,
                allow_quantity=True,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_markup=5_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=50_000)
            session.add_all([product, user])
            await session.commit()

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            2,
            supplier,  # type: ignore[arg-type]
            supplier_idempotency_key="shop-api-pending-recovery",
        )

        repeated = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            2,
            supplier,  # type: ignore[arg-type]
            supplier_idempotency_key="shop-api-pending-recovery",
        )

        assert result.ok is False
        assert result.message == "supplier_unavailable"
        assert repeated.ok is False
        assert repeated.message == "supplier_unavailable"
        assert supplier.buy_calls == 1
        async with sessions() as session:
            stored_user = await session.get(User, user.telegram_id)
            recovery = await session.scalar(select(SupplierRecoveryRequest))
            assert stored_user is not None and stored_user.balance == 50_000
            assert recovery is not None and recovery.status == "pending"
            assert recovery.product_id == product.id
            assert recovery.supplier_product_id == "SP-GEF55PBV"
            assert recovery.quantity == 2
        await engine.dispose()

    asyncio.run(scenario())


def test_recovered_supplier_inventory_is_sold_before_buying_again() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = FakeSupplier(balance=0, stock=0)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="ChatGPT Plus",
                name_en="ChatGPT Plus",
                price=20_000,
                allow_quantity=True,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_markup=5_000,
                external_stock=2,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=50_000)
            session.add_all([product, user])
            await session.flush()
            session.add_all(
                [
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt(f"orphan{index}:password"),
                        cost_amount=15_000,
                        supplier_order_code="API-TELE-ORPHAN",
                        supplier_provider="sumistore",
                        supplier_item_index=index,
                    )
                    for index in range(2)
                ]
            )
            await session.commit()

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            2,
            supplier,  # type: ignore[arg-type]
        )

        assert result.ok is True
        assert supplier.buy_calls == 0
        assert result.secrets == ["orphan0:password", "orphan1:password"]
        async with sessions() as session:
            orders = list(await session.scalars(select(Order).order_by(Order.id)))
            assert all(order.cost_amount == 15_000 for order in orders)
            assert all(order.supplier_order_code == "API-TELE-ORPHAN" for order in orders)
            assert all(order.supplier_provider == "sumistore" for order in orders)
        await engine.dispose()

    asyncio.run(scenario())


def test_last_locked_inventory_item_releases_dynamic_price() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = FakeSupplier(balance=100_000, stock=100)
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Hàng ôm",
                name_en="Stocked item",
                price=28_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_price=27_000,
                supplier_markup=8_000,
                price_lock_enabled=True,
                external_stock=1,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=28_000)
            session.add_all([product, user])
            await session.flush()
            session.add(
                InventoryItem(
                    product_id=product.id,
                    encrypted_secret=cipher.encrypt("stocked:password"),
                    cost_amount=20_000,
                )
            )
            await session.commit()

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            1,
            supplier,  # type: ignore[arg-type]
        )

        assert result.ok is True
        assert result.total_amount == 28_000
        assert supplier.buy_calls == 0
        async with sessions() as session:
            stored_product = await session.get(Product, product.id)
            assert stored_product is not None
            assert stored_product.price_lock_enabled is False
            assert stored_product.price == 35_000
            assert stored_product.external_stock == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_locked_inventory_can_fill_missing_quantity_from_api() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = FakeSupplier(balance=1_000_000, stock=100)
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Hàng ôm",
                name_en="Stocked item",
                price=28_000,
                allow_quantity=True,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_price=20_000,
                supplier_markup=8_000,
                price_lock_enabled=True,
                external_stock=1,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=100_000)
            session.add_all([product, user])
            await session.flush()
            session.add(
                InventoryItem(
                    product_id=product.id,
                    encrypted_secret=cipher.encrypt("stocked:password"),
                    cost_amount=20_000,
                )
            )
            await session.commit()

        result = await purchase_product(
            sessions,
            user.telegram_id,
            product.id,
            cipher,
            2,
            supplier,  # type: ignore[arg-type]
        )

        assert result.ok is True
        assert result.total_amount == 56_000
        assert supplier.buy_calls == 1
        assert supplier.buy_quantities == [1]
        async with sessions() as session:
            stored_product = await session.get(Product, product.id)
            available_items = int(
                await session.scalar(
                    select(func.count(InventoryItem.id)).where(
                        InventoryItem.product_id == product.id,
                        InventoryItem.status == "available",
                    )
                )
                or 0
            )
            assert stored_product is not None and stored_product.price_lock_enabled is False
            assert stored_product.external_stock == 65
            assert available_items == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_external_stock_is_zero_when_supplier_balance_is_insufficient() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = FakeSupplier(balance=0, stock=100)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="ChatGPT Plus",
                name_en="ChatGPT Plus",
                price=20_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_markup=5_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=100_000)
            session.add_all([product, user])
            await session.commit()

        result = await purchase_product(
            sessions,
            123456,
            product.id,
            cipher,
            1,
            supplier,  # type: ignore[arg-type]
        )
        assert result.ok is False
        assert result.message == "out_of_stock"
        assert supplier.buy_calls == 0
        async with sessions() as session:
            product = await session.get(Product, product.id)
            assert product is not None and product.external_stock == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_external_direct_payment_delivers_supplier_account() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = FakeSupplier(balance=100_000, stock=100)
        fulfillment_events: list[tuple[int, str]] = []
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="ChatGPT Plus",
                name_en="ChatGPT Plus",
                price=20_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_markup=5_000,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=0)
            session.add_all([product, user])
            await session.flush()
            session.add(
                Deposit(
                    user_id=user.telegram_id,
                    code="NAP123456ABCD",
                    requested_amount=20_000,
                    payment_kind="direct_purchase",
                    product_id=product.id,
                )
            )
            await session.commit()

        result = await process_sepay_payment(
            sessions,
            {
                "id": 44444,
                "transferType": "in",
                "transferAmount": 20_000,
                "content": "NAP123456ABCD",
            },
            cipher=cipher,
            supplier_client=supplier,  # type: ignore[arg-type]
            on_fulfillment_started=lambda user_id, language: _record_fulfillment_event(
                fulfillment_events,
                user_id,
                language,
            ),
        )
        assert result.status == "direct_purchase_completed"
        assert fulfillment_events == [(123456, "vi")]
        assert [cipher.decrypt(value) for value in result.encrypted_secrets] == [
            "chatgpt1:password"
        ]
        async with sessions() as session:
            order = await session.scalar(select(Order))
            assert order is not None and order.amount == 20_000
            assert order.cost_amount == 15_000
            assert order.supplier_order_code == "API-TELE-TEST123"
            assert order.supplier_provider == "sumistore"
        await engine.dispose()

    asyncio.run(scenario())


async def _record_fulfillment_event(
    events: list[tuple[int, str]],
    user_id: int,
    language: str,
) -> None:
    events.append((user_id, language))


def test_external_direct_payment_uses_recovered_inventory_first() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = FakeSupplier(balance=0, stock=0)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="ChatGPT Plus",
                name_en="ChatGPT Plus",
                price=20_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_markup=5_000,
                external_stock=1,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=0)
            session.add_all([product, user])
            await session.flush()
            session.add(
                InventoryItem(
                    product_id=product.id,
                    encrypted_secret=cipher.encrypt("recovered|password"),
                    cost_amount=12_000,
                    supplier_order_code="API-TELE-ORPHAN",
                    supplier_provider="sumistore",
                    supplier_item_index=0,
                )
            )
            session.add(
                Deposit(
                    user_id=user.telegram_id,
                    code="NAP123456ABCD",
                    requested_amount=20_000,
                    payment_kind="direct_purchase",
                    product_id=product.id,
                )
            )
            await session.commit()

        result = await process_sepay_payment(
            sessions,
            {
                "id": 44444,
                "transferType": "in",
                "transferAmount": 20_000,
                "content": "NAP123456ABCD",
            },
            cipher=cipher,
            supplier_client=supplier,  # type: ignore[arg-type]
        )

        assert result.status == "direct_purchase_completed"
        assert supplier.buy_calls == 0
        assert [cipher.decrypt(value) for value in result.encrypted_secrets] == [
            "recovered|password"
        ]
        async with sessions() as session:
            order = await session.scalar(select(Order))
            assert order is not None and order.cost_amount == 12_000
            assert order.supplier_order_code == "API-TELE-ORPHAN"
            assert order.supplier_provider == "sumistore"
        await engine.dispose()

    asyncio.run(scenario())


def test_locked_inventory_qr_never_falls_through_to_supplier_api() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = FakeSupplier(balance=1_000_000, stock=100)
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Hàng ôm",
                name_en="Stocked item",
                price=28_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_price=20_000,
                supplier_markup=8_000,
                price_lock_enabled=True,
                external_stock=1,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=0)
            session.add_all([product, user])
            await session.flush()
            item = InventoryItem(
                product_id=product.id,
                encrypted_secret=cipher.encrypt("stocked:password"),
                cost_amount=20_000,
            )
            session.add(item)
            await session.commit()

            deposit = await create_deposit(
                session,
                user.telegram_id,
                28_000,
                payment_kind="direct_purchase",
                product_id=product.id,
            )
            assert deposit.inventory_price_locked is True

            item.status = "sold"
            product.price_lock_enabled = False
            product.external_stock = 0
            await session.commit()

        result = await process_sepay_payment(
            sessions,
            {
                "id": 55555,
                "transferType": "in",
                "transferAmount": 28_000,
                "content": deposit.code,
            },
            cipher=cipher,
            supplier_client=supplier,  # type: ignore[arg-type]
        )

        assert result.status == "direct_purchase_fallback"
        assert supplier.buy_calls == 0
        async with sessions() as session:
            stored_user = await session.get(User, user.telegram_id)
            assert stored_user is not None and stored_user.balance == 28_000
        await engine.dispose()

    asyncio.run(scenario())


def test_locked_inventory_qr_can_use_supplier_stock() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = FakeSupplier(balance=1_000_000, stock=100)
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Stocked item",
                name_en="Stocked item",
                price=28_000,
                allow_quantity=True,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_price=20_000,
                supplier_markup=8_000,
                price_lock_enabled=True,
                external_stock=1,
            )
            user = User(telegram_id=123456, full_name="Buyer", balance=0)
            session.add_all([product, user])
            await session.flush()
            session.add(
                InventoryItem(
                    product_id=product.id,
                    encrypted_secret=cipher.encrypt("stocked:password"),
                    cost_amount=20_000,
                )
            )
            await session.commit()

            deposit = await create_deposit(
                session,
                user.telegram_id,
                56_000,
                payment_kind="direct_purchase",
                product_id=product.id,
                quantity=2,
            )
            assert deposit.inventory_price_locked is True

        result = await process_sepay_payment(
            sessions,
            {
                "id": 55556,
                "transferType": "in",
                "transferAmount": 56_000,
                "content": deposit.code,
            },
            cipher=cipher,
            supplier_client=supplier,  # type: ignore[arg-type]
        )

        assert result.status == "direct_purchase_completed"
        assert result.quantity == 2
        assert supplier.buy_calls == 1
        assert supplier.buy_quantities == [1]
        async with sessions() as session:
            stored_product = await session.get(Product, product.id)
            available_items = int(
                await session.scalar(
                    select(func.count(InventoryItem.id)).where(
                        InventoryItem.product_id == product.id,
                        InventoryItem.status == "available",
                    )
                )
                or 0
            )
            assert stored_product is not None
            assert stored_product.price_lock_enabled is False
            assert stored_product.external_stock == 65
            assert available_items == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_seller_qr_uses_local_stock_then_buys_only_missing_quantity() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        supplier = FakeSupplier(balance=1_000_000, stock=100)
        async with sessions() as session:
            category = Category(name_vi="API", name_en="API")
            seller = User(telegram_id=73002, full_name="Seller", balance=0)
            session.add_all([category, seller])
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Seller QR mixed",
                name_en="Seller QR mixed",
                price=28_000,
                allow_quantity=True,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
                supplier_price=15_000,
                supplier_markup=13_000,
                external_stock=2,
            )
            session.add(product)
            await session.flush()
            rule = SellerPrice(
                user_id=seller.telegram_id,
                product_id=product.id,
                profit_per_unit=5_000,
            )
            session.add(rule)
            session.add(
                InventoryItem(
                    product_id=product.id,
                    encrypted_secret=cipher.encrypt("local-qr|password"),
                    cost_amount=20_000,
                )
            )
            await session.flush()
            deposit = await create_deposit(
                session,
                seller.telegram_id,
                45_000,
                payment_kind="direct_purchase",
                product_id=product.id,
                quantity=2,
                seller_price_id=rule.id,
                seller_profit_per_unit=5_000,
            )

        result = await process_sepay_payment(
            sessions,
            {
                "id": 55557,
                "transferType": "in",
                "transferAmount": 45_000,
                "content": deposit.code,
            },
            cipher=cipher,
            supplier_client=supplier,  # type: ignore[arg-type]
        )

        assert result.status == "direct_purchase_completed"
        assert supplier.buy_quantities == [1]
        async with sessions() as session:
            orders = list(
                await session.scalars(
                    select(Order).where(Order.user_id == seller.telegram_id).order_by(Order.id)
                )
            )
            assert [order.amount for order in orders] == [25_000, 20_000]
            assert [order.cost_amount for order in orders] == [20_000, 15_000]
        await engine.dispose()

    asyncio.run(scenario())


def test_direct_qr_purchase_pays_referral_commission() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            referrer = User(telegram_id=70001, full_name="Referrer", balance=0)
            buyer = User(
                telegram_id=70002,
                full_name="Buyer",
                balance=0,
                referred_by_id=referrer.telegram_id,
            )
            category = Category(name_vi="Tài khoản", name_en="Accounts")
            session.add_all([referrer, buyer, category])
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tài khoản QR",
                name_en="QR account",
                price=20_000,
            )
            session.add(product)
            await session.flush()
            session.add(
                InventoryItem(
                    product_id=product.id,
                    encrypted_secret=cipher.encrypt("qr-account|password"),
                )
            )
            session.add(
                Deposit(
                    user_id=buyer.telegram_id,
                    code="NAP70002ABCD",
                    requested_amount=20_000,
                    payment_kind="direct_purchase",
                    product_id=product.id,
                )
            )
            await session.commit()

        result = await process_sepay_payment(
            sessions,
            {
                "id": 70002001,
                "transferType": "in",
                "transferAmount": 20_000,
                "content": "NAP70002ABCD",
            },
            cipher=cipher,
            referral_commission_percent=5,
        )
        assert result.status == "direct_purchase_completed"
        async with sessions() as session:
            referrer = await session.get(User, 70001)
            reward = await session.scalar(select(ReferralReward))
            assert referrer is not None and referrer.balance == 1_000
            assert reward is not None and reward.order_amount == 20_000
            assert reward.commission_amount == 1_000
            assert reward.sales_channel == "telegram"
        await engine.dispose()

    asyncio.run(scenario())
