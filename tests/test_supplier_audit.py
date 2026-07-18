import asyncio

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import (
    Category,
    Deposit,
    InventoryItem,
    Product,
    SupplierBalanceState,
    SupplierBalanceTransaction,
    User,
)
from app.services import process_sepay_payment, purchase_product
from app.supplier_audit import recover_supplier_order, reconcile_supplier_balance
from app.suppliers import SupplierPurchase, SupplierSnapshot
from app.utils import SecretCipher


class ConcurrentSupplier:
    def __init__(self, balance: int = 100_000) -> None:
        self.balance = balance
        self.balance_lock = asyncio.Lock()
        self.buy_count = 0
        self.in_flight = 0
        self.max_in_flight = 0

    async def fetch_balance(self) -> int:
        return self.balance

    async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
        return SupplierSnapshot(
            product_id=product_id,
            name="Concurrent test product",
            description="",
            unit_price=15_000,
            source_stock=100,
            owner_balance=self.balance,
        )

    async def buy(self, product_id: str, quantity: int) -> SupplierPurchase:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.02)
            cost = 15_000 * quantity
            assert self.balance >= cost
            self.balance -= cost
            self.buy_count += 1
            return SupplierPurchase(
                order_code=f"API-CONCURRENT-{self.buy_count}",
                unit_price=15_000,
                accounts=tuple(
                    f"account-{self.buy_count}-{index}|password"
                    for index in range(1, quantity + 1)
                ),
            )
        finally:
            self.in_flight -= 1


class RecoverySupplier:
    async def fetch_order(self, order_code: str) -> SupplierPurchase:
        return SupplierPurchase(
            order_code=order_code,
            unit_price=12_000,
            accounts=("recovered1|password", "recovered2|password"),
            product_id="SP-RECOVERY",
        )


async def make_database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_unmatched_supplier_balance_drop_is_recorded_once() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        supplier = ConcurrentSupplier(balance=100_000)

        baseline = await reconcile_supplier_balance(sessions, supplier)  # type: ignore[arg-type]
        assert baseline.initialized is True

        supplier.balance = 87_000
        detected = await reconcile_supplier_balance(sessions, supplier)  # type: ignore[arg-type]
        repeated = await reconcile_supplier_balance(sessions, supplier)  # type: ignore[arg-type]

        assert detected.suspicious_amount == -13_000
        assert detected.observed_delta == -13_000
        assert repeated.suspicious_amount == 0
        async with sessions() as session:
            transactions = list(
                await session.scalars(
                    select(SupplierBalanceTransaction).order_by(
                        SupplierBalanceTransaction.id
                    )
                )
            )
            assert len(transactions) == 1
            assert transactions[0].kind == "suspicious"
            assert transactions[0].amount == -13_000
            assert transactions[0].balance_before == 100_000
            assert transactions[0].balance_after == 87_000
        await engine.dispose()

    asyncio.run(scenario())


def test_lehai_reconciliation_is_isolated_and_detects_unmatched_drop() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        sumi = ConcurrentSupplier(balance=100_000)
        lehai = ConcurrentSupplier(balance=80_000)

        await reconcile_supplier_balance(sessions, sumi)  # type: ignore[arg-type]
        baseline = await reconcile_supplier_balance(
            sessions,
            lehai,  # type: ignore[arg-type]
            provider="lehai",
            provider_label="Le Hai Premium",
        )
        assert baseline.initialized is True

        async with sessions() as session:
            session.add(
                SupplierBalanceTransaction(
                    provider="lehai",
                    kind="purchase",
                    amount=-15_000,
                    shop_order_code="B-LEHAI-1",
                )
            )
            await session.commit()

        lehai.balance = 65_000
        matched = await reconcile_supplier_balance(
            sessions,
            lehai,  # type: ignore[arg-type]
            provider="lehai",
            provider_label="Le Hai Premium",
        )
        assert matched.expected_purchase_debit == 15_000
        assert matched.suspicious_amount == 0

        lehai.balance = 60_000
        suspicious = await reconcile_supplier_balance(
            sessions,
            lehai,  # type: ignore[arg-type]
            provider="lehai",
            provider_label="Le Hai Premium",
        )
        assert suspicious.suspicious_amount == -5_000

        async with sessions() as session:
            sumi_state = await session.get(SupplierBalanceState, "sumistore")
            lehai_state = await session.get(SupplierBalanceState, "lehai")
            transactions = list(
                await session.scalars(
                    select(SupplierBalanceTransaction).order_by(
                        SupplierBalanceTransaction.id
                    )
                )
            )
            assert sumi_state is not None and sumi_state.last_balance == 100_000
            assert lehai_state is not None and lehai_state.last_balance == 60_000
            assert [transaction.provider for transaction in transactions] == [
                "lehai",
                "lehai",
            ]
            assert [transaction.kind for transaction in transactions] == [
                "purchase",
                "suspicious",
            ]
            assert transactions[-1].amount == -5_000
        await engine.dispose()

    asyncio.run(scenario())


def test_simultaneous_wallet_and_qr_purchases_do_not_create_false_alerts() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        supplier = ConcurrentSupplier(balance=100_000)
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="Tài khoản", name_en="Accounts")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tài khoản API",
                name_en="API account",
                price=20_000,
                allow_quantity=True,
                fulfillment_source="sumistore",
                supplier_product_id="SP-CONCURRENT",
                supplier_markup=5_000,
            )
            wallet_user = User(telegram_id=10001, full_name="Wallet buyer", balance=20_000)
            qr_user = User(telegram_id=10002, full_name="QR buyer", balance=0)
            session.add_all([product, wallet_user, qr_user])
            await session.flush()
            session.add(
                Deposit(
                    user_id=qr_user.telegram_id,
                    code="NAP10002ABCD",
                    requested_amount=20_000,
                    payment_kind="direct_purchase",
                    product_id=product.id,
                )
            )
            await session.commit()

        await reconcile_supplier_balance(sessions, supplier)  # type: ignore[arg-type]
        wallet_result, qr_result = await asyncio.gather(
            purchase_product(
                sessions,
                wallet_user.telegram_id,
                product.id,
                cipher,
                supplier_client=supplier,  # type: ignore[arg-type]
            ),
            process_sepay_payment(
                sessions,
                {
                    "id": 90001,
                    "transferType": "in",
                    "transferAmount": 20_000,
                    "content": "NAP10002ABCD",
                },
                cipher=cipher,
                supplier_client=supplier,  # type: ignore[arg-type]
            ),
        )

        assert wallet_result.ok is True
        assert qr_result.status == "direct_purchase_completed"
        assert supplier.buy_count == 2
        assert supplier.max_in_flight == 1
        assert supplier.balance == 70_000

        reconciled = await reconcile_supplier_balance(sessions, supplier)  # type: ignore[arg-type]
        assert reconciled.expected_purchase_debit == 30_000
        assert reconciled.observed_delta == -30_000
        assert reconciled.suspicious_amount == 0
        async with sessions() as session:
            transactions = list(
                await session.scalars(
                    select(SupplierBalanceTransaction).order_by(
                        SupplierBalanceTransaction.id
                    )
                )
            )
            assert [transaction.kind for transaction in transactions] == [
                "purchase",
                "purchase",
            ]
            assert sum(transaction.amount for transaction in transactions) == -30_000
            assert len({transaction.shop_order_code for transaction in transactions}) == 2
        await engine.dispose()

    asyncio.run(scenario())


def test_suspicious_supplier_order_can_be_recovered_into_stock_once() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Recovered",
                name_en="Recovered",
                price=17_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-RECOVERY",
            )
            audit = SupplierBalanceTransaction(
                provider="sumistore",
                kind="suspicious",
                amount=-24_000,
                balance_before=64_000,
                balance_after=40_000,
            )
            session.add_all([product, audit])
            await session.commit()

        async with sessions() as session:
            async with session.begin():
                first = await recover_supplier_order(
                    session,
                    RecoverySupplier(),  # type: ignore[arg-type]
                    cipher,
                    audit_transaction_id=audit.id,
                    product_id=product.id,
                    supplier_order_code="API-RECOVERED",
                )
        async with sessions() as session:
            async with session.begin():
                repeated = await recover_supplier_order(
                    session,
                    RecoverySupplier(),  # type: ignore[arg-type]
                    cipher,
                    audit_transaction_id=audit.id,
                    product_id=product.id,
                    supplier_order_code="API-RECOVERED",
                )

        assert first.inserted_count == 2
        assert repeated.inserted_count == 0
        async with sessions() as session:
            items = list(await session.scalars(select(InventoryItem).order_by(InventoryItem.id)))
            audit = await session.get(SupplierBalanceTransaction, audit.id)
            assert len(items) == 2
            assert [cipher.decrypt(item.encrypted_secret) for item in items] == [
                "recovered1|password",
                "recovered2|password",
            ]
            assert all(item.cost_amount == 12_000 for item in items)
            assert audit is not None and audit.kind == "recovered"
            assert audit.supplier_order_code == "API-RECOVERED"
        await engine.dispose()

    asyncio.run(scenario())
