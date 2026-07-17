import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import SupplierBalanceState, SupplierBalanceTransaction
from app.suppliers import SumistoreClient, supplier_balance_guard


logger = logging.getLogger(__name__)
PROVIDER = "sumistore"


@dataclass(frozen=True)
class SupplierReconcileResult:
    current_balance: int
    initialized: bool = False
    observed_delta: int = 0
    expected_purchase_debit: int = 0
    unexplained_delta: int = 0
    suspicious_transaction_id: int | None = None

    @property
    def suspicious_amount(self) -> int:
        return min(0, self.unexplained_delta)


def record_supplier_purchase(
    session: AsyncSession,
    *,
    amount: int,
    supplier_order_code: str | None,
    shop_order_code: str,
    product_id: int,
    quantity: int,
) -> SupplierBalanceTransaction:
    transaction = SupplierBalanceTransaction(
        provider=PROVIDER,
        kind="purchase",
        amount=-max(0, amount),
        supplier_order_code=supplier_order_code,
        shop_order_code=shop_order_code,
        product_id=product_id,
        quantity=quantity,
        note="Chi phí mua hàng do shop tạo qua API Sumi.",
    )
    session.add(transaction)
    return transaction


async def reconcile_supplier_balance(
    session_factory: async_sessionmaker[AsyncSession],
    client: SumistoreClient,
) -> SupplierReconcileResult:
    async with supplier_balance_guard(client):
        current_balance = await client.fetch_balance()
        checked_at = datetime.now(UTC)
        async with session_factory() as session:
            async with session.begin():
                state = await session.scalar(
                    select(SupplierBalanceState)
                    .where(SupplierBalanceState.provider == PROVIDER)
                    .with_for_update()
                )
                latest_purchase_id = int(
                    await session.scalar(
                        select(func.coalesce(func.max(SupplierBalanceTransaction.id), 0)).where(
                            SupplierBalanceTransaction.provider == PROVIDER,
                            SupplierBalanceTransaction.kind == "purchase",
                        )
                    )
                    or 0
                )
                if state is None or state.last_balance is None:
                    if state is None:
                        state = SupplierBalanceState(provider=PROVIDER)
                        session.add(state)
                    state.last_balance = current_balance
                    state.last_purchase_id = latest_purchase_id
                    state.checked_at = checked_at
                    return SupplierReconcileResult(current_balance, initialized=True)

                purchases = list(
                    await session.scalars(
                        select(SupplierBalanceTransaction).where(
                            SupplierBalanceTransaction.provider == PROVIDER,
                            SupplierBalanceTransaction.kind == "purchase",
                            SupplierBalanceTransaction.id > state.last_purchase_id,
                        )
                    )
                )
                expected_purchase_debit = sum(-transaction.amount for transaction in purchases)
                observed_delta = current_balance - state.last_balance
                unexplained_delta = observed_delta + expected_purchase_debit
                transaction: SupplierBalanceTransaction | None = None
                if unexplained_delta < 0:
                    transaction = SupplierBalanceTransaction(
                        provider=PROVIDER,
                        kind="suspicious",
                        amount=unexplained_delta,
                        balance_before=state.last_balance,
                        balance_after=current_balance,
                        note=(
                            "Số dư Sumi giảm nhiều hơn tổng chi phí các đơn do shop ghi nhận "
                            "trong cùng kỳ đối soát."
                        ),
                        period_started_at=state.checked_at,
                        created_at=checked_at,
                    )
                    session.add(transaction)
                    await session.flush()
                    logger.warning(
                        "Suspicious Sumi balance decrease detected: amount=%s before=%s after=%s",
                        unexplained_delta,
                        state.last_balance,
                        current_balance,
                    )
                elif unexplained_delta > 0:
                    session.add(
                        SupplierBalanceTransaction(
                            provider=PROVIDER,
                            kind="credit",
                            amount=unexplained_delta,
                            balance_before=state.last_balance,
                            balance_after=current_balance,
                            note="Số dư Sumi tăng ngoài các đơn mua của shop.",
                            period_started_at=state.checked_at,
                            created_at=checked_at,
                        )
                    )

                state.last_balance = current_balance
                state.last_purchase_id = latest_purchase_id
                state.checked_at = checked_at
                return SupplierReconcileResult(
                    current_balance=current_balance,
                    observed_delta=observed_delta,
                    expected_purchase_debit=expected_purchase_debit,
                    unexplained_delta=unexplained_delta,
                    suspicious_transaction_id=transaction.id if transaction else None,
                )
