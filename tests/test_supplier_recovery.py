import asyncio
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import (
    Category,
    InventoryItem,
    Product,
    SupplierBalanceTransaction,
    SupplierRecoveryRequest,
)
from app.supplier_recovery import (
    queue_supplier_recovery,
    recover_pending_sumistore_orders,
)
from app.suppliers import SupplierOrderSummary, SupplierPurchase
from app.utils import SecretCipher


async def make_database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


class DelayedSupplier:
    provider = "sumistore"

    def __init__(self, created_at: datetime) -> None:
        self.balance_lock = asyncio.Lock()
        self.created_at = created_at

    async def fetch_orders(self) -> tuple[SupplierOrderSummary, ...]:
        return (
            SupplierOrderSummary(
                order_code="API-DELAYED-RECOVERY",
                product_id="SP-GEF55PBV",
                quantity=2,
                created_at=self.created_at,
            ),
            SupplierOrderSummary(
                order_code="API-UNRELATED",
                product_id="SP-GEF55PBV",
                quantity=1,
                created_at=self.created_at,
            ),
        )

    async def fetch_order(self, order_code: str) -> SupplierPurchase:
        assert order_code == "API-DELAYED-RECOVERY"
        return SupplierPurchase(
            order_code=order_code,
            unit_price=10_000,
            accounts=("late1|password", "late2|password"),
            product_id="SP-GEF55PBV",
        )


def test_delayed_sumistore_order_is_imported_and_links_suspicious_audit() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        started_at = datetime.now(UTC)
        source_created_at = started_at + timedelta(minutes=2)
        async with sessions() as session:
            category = Category(name_vi="ChatGPT", name_en="ChatGPT")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="GPT Plus",
                name_en="GPT Plus",
                price=15_000,
                fulfillment_source="sumistore",
                supplier_product_id="SP-GEF55PBV",
            )
            session.add(product)
            await session.flush()
            await queue_supplier_recovery(
                session,
                provider="sumistore",
                supplier_product_id="SP-GEF55PBV",
                quantity=2,
                request_key="shop-delayed-test",
                started_at=started_at,
                error_code="SUPPLIER_UNAVAILABLE",
            )
            audit = SupplierBalanceTransaction(
                provider="sumistore",
                kind="suspicious",
                amount=-20_000,
                balance_before=100_000,
                balance_after=80_000,
                period_started_at=started_at - timedelta(seconds=1),
                created_at=source_created_at + timedelta(seconds=1),
            )
            session.add(audit)
            await session.commit()

        supplier = DelayedSupplier(source_created_at)
        first = await recover_pending_sumistore_orders(
            sessions,
            supplier,  # type: ignore[arg-type]
            cipher,
        )
        second = await recover_pending_sumistore_orders(
            sessions,
            supplier,  # type: ignore[arg-type]
            cipher,
        )

        assert first.matched_orders == 1
        assert first.inserted_accounts == 2
        assert first.linked_audits == 1
        assert second.matched_orders == 0
        assert second.inserted_accounts == 0
        async with sessions() as session:
            recovery = await session.scalar(select(SupplierRecoveryRequest))
            items = list(
                await session.scalars(select(InventoryItem).order_by(InventoryItem.id))
            )
            stored_audit = await session.get(SupplierBalanceTransaction, audit.id)
            assert recovery is not None and recovery.status == "recovered"
            assert recovery.supplier_order_code == "API-DELAYED-RECOVERY"
            assert recovery.audit_transaction_id == audit.id
            assert len(items) == 2
            assert [cipher.decrypt(item.encrypted_secret) for item in items] == [
                "late1|password",
                "late2|password",
            ]
            assert all(item.status == "available" for item in items)
            assert all(item.cost_amount == 10_000 for item in items)
            assert stored_audit is not None and stored_audit.kind == "recovered"
            assert stored_audit.quantity == 2
        await engine.dispose()

    asyncio.run(scenario())
