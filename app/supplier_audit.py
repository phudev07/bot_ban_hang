import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    InventoryItem,
    Product,
    SupplierBalanceState,
    SupplierBalanceTransaction,
    SupplierRecoveryRequest,
)
from app.suppliers import ExternalSupplierClient, SumistoreClient, supplier_balance_guard
from app.utils import SecretCipher


logger = logging.getLogger(__name__)
PROVIDER = "sumistore"
SUPPLIER_ALERT_GRACE_PERIODS = {
    "sumistore": timedelta(hours=24),
    "lehai": timedelta(hours=48),
}


@dataclass(frozen=True)
class SupplierReconcileResult:
    current_balance: int
    initialized: bool = False
    observed_delta: int = 0
    expected_purchase_debit: int = 0
    unexplained_delta: int = 0
    suspicious_transaction_id: int | None = None
    refunded_amount: int = 0
    refunded_audit_ids: tuple[int, ...] = ()

    @property
    def suspicious_amount(self) -> int:
        return min(0, self.unexplained_delta)


@dataclass(frozen=True)
class SupplierOrderRecoveryResult:
    order_code: str
    account_count: int
    inserted_count: int
    total_cost: int


@dataclass(frozen=True)
class UnresolvedSupplierAlert:
    transaction_id: int
    provider: str
    amount: int
    balance_before: int | None
    balance_after: int | None
    period_started_at: datetime | None
    created_at: datetime


async def pending_unresolved_supplier_alerts(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    provider: str,
    now: datetime | None = None,
    limit: int = 20,
) -> tuple[UnresolvedSupplierAlert, ...]:
    checked_at = now or datetime.now(UTC)
    grace_period = SUPPLIER_ALERT_GRACE_PERIODS.get(provider, timedelta(hours=24))
    cutoff = checked_at - grace_period
    async with session_factory() as session:
        transactions = list(
            await session.scalars(
                select(SupplierBalanceTransaction)
                .where(
                    SupplierBalanceTransaction.provider == provider,
                    SupplierBalanceTransaction.kind == "suspicious",
                    SupplierBalanceTransaction.admin_alerted_at.is_(None),
                    SupplierBalanceTransaction.created_at < cutoff,
                )
                .order_by(SupplierBalanceTransaction.id)
            )
        )
        if provider == PROVIDER and transactions:
            active_recoveries = list(
                await session.scalars(
                    select(SupplierRecoveryRequest).where(
                        SupplierRecoveryRequest.provider == provider,
                        SupplierRecoveryRequest.status == "pending",
                        SupplierRecoveryRequest.expires_at >= checked_at,
                    )
                )
            )
        else:
            active_recoveries = []

    alerts: list[UnresolvedSupplierAlert] = []
    for transaction in transactions:
        has_active_recovery = any(
            recovery.audit_transaction_id == transaction.id
            or recovery.request_key.startswith(f"audit-{transaction.id}-")
            for recovery in active_recoveries
        )
        if has_active_recovery:
            continue
        alerts.append(
            UnresolvedSupplierAlert(
                transaction_id=transaction.id,
                provider=transaction.provider,
                amount=transaction.amount,
                balance_before=transaction.balance_before,
                balance_after=transaction.balance_after,
                period_started_at=transaction.period_started_at,
                created_at=transaction.created_at,
            )
        )
        if len(alerts) >= max(1, limit):
            break
    return tuple(alerts)


async def mark_supplier_alerted(
    session_factory: async_sessionmaker[AsyncSession],
    transaction_id: int,
    *,
    alerted_at: datetime | None = None,
) -> bool:
    async with session_factory() as session:
        async with session.begin():
            transaction = await session.scalar(
                select(SupplierBalanceTransaction)
                .where(SupplierBalanceTransaction.id == transaction_id)
                .with_for_update()
            )
            if (
                transaction is None
                or transaction.kind != "suspicious"
                or transaction.admin_alerted_at is not None
            ):
                return False
            transaction.admin_alerted_at = alerted_at or datetime.now(UTC)
            return True


async def recover_supplier_order(
    session: AsyncSession,
    client: SumistoreClient,
    cipher: SecretCipher,
    *,
    audit_transaction_id: int,
    product_id: int,
    supplier_order_code: str,
) -> SupplierOrderRecoveryResult:
    purchase = await client.fetch_order(supplier_order_code)
    audit = await session.scalar(
        select(SupplierBalanceTransaction)
        .where(SupplierBalanceTransaction.id == audit_transaction_id)
        .with_for_update()
    )
    product = await session.get(Product, product_id)
    if audit is None or audit.kind not in {"suspicious", "recovered"}:
        raise ValueError("Supplier audit transaction is not recoverable")
    if product is None or product.fulfillment_source != "sumistore":
        raise ValueError("Supplier product is not recoverable")
    if purchase.product_id and purchase.product_id != product.supplier_product_id:
        raise ValueError("Supplier order belongs to another product")

    total_cost = purchase.unit_price * len(purchase.accounts)
    if audit.amount >= 0 or abs(audit.amount) != total_cost:
        raise ValueError("Supplier order cost does not match the suspicious debit")

    inserted_count = 0
    for item_index, account in enumerate(purchase.accounts):
        existing = await session.scalar(
            select(InventoryItem.id).where(
                InventoryItem.supplier_order_code == purchase.order_code,
                InventoryItem.supplier_item_index == item_index,
            )
        )
        if existing is not None:
            continue
        session.add(
            InventoryItem(
                product_id=product.id,
                encrypted_secret=cipher.encrypt(account),
                cost_amount=purchase.unit_price,
                supplier_order_code=purchase.order_code,
                supplier_item_index=item_index,
                status="available",
            )
        )
        inserted_count += 1

    audit.kind = "recovered"
    audit.supplier_order_code = purchase.order_code
    audit.product_id = product.id
    audit.quantity = len(purchase.accounts)
    audit.note = (
        "Đã thu hồi tài khoản từ đơn Sumi hoàn tất sau khi shop bị timeout. "
        "Hàng đã được mã hóa và nhập lại kho để bán tiếp."
    )
    await session.flush()
    return SupplierOrderRecoveryResult(
        order_code=purchase.order_code,
        account_count=len(purchase.accounts),
        inserted_count=inserted_count,
        total_cost=total_cost,
    )


def record_supplier_purchase(
    session: AsyncSession,
    *,
    amount: int,
    supplier_order_code: str | None,
    shop_order_code: str,
    product_id: int,
    quantity: int,
    provider: str = PROVIDER,
) -> SupplierBalanceTransaction:
    provider_name = "Sumi" if provider == PROVIDER else "Lê Hải Premium"
    transaction = SupplierBalanceTransaction(
        provider=provider,
        kind="purchase",
        amount=-max(0, amount),
        supplier_order_code=supplier_order_code,
        shop_order_code=shop_order_code,
        product_id=product_id,
        quantity=quantity,
        note=f"Chi phí mua hàng do shop tạo qua API {provider_name}.",
    )
    session.add(transaction)
    return transaction


async def match_supplier_refund(
    session: AsyncSession,
    *,
    provider: str,
    amount: int,
    refunded_at: datetime,
) -> tuple[SupplierBalanceTransaction, ...]:
    if provider != "lehai" or amount <= 0:
        return ()
    candidates = list(
        await session.scalars(
            select(SupplierBalanceTransaction)
            .where(
                SupplierBalanceTransaction.provider == provider,
                SupplierBalanceTransaction.kind == "suspicious",
                SupplierBalanceTransaction.amount < 0,
                SupplierBalanceTransaction.created_at
                >= refunded_at - timedelta(hours=48),
                SupplierBalanceTransaction.created_at <= refunded_at,
                SupplierBalanceTransaction.supplier_order_code.is_(None),
                SupplierBalanceTransaction.shop_order_code.is_(None),
            )
            .order_by(SupplierBalanceTransaction.id.desc())
            .with_for_update()
        )
    )
    exact = next(
        (transaction for transaction in candidates if abs(transaction.amount) == amount),
        None,
    )
    if exact is not None:
        matched = [exact]
    else:
        matched = []
        remaining = amount
        for transaction in candidates:
            debit = abs(transaction.amount)
            if debit <= remaining:
                matched.append(transaction)
                remaining -= debit
            if remaining == 0:
                break
        if remaining != 0:
            return ()

    for transaction in matched:
        transaction.kind = "refunded"
        transaction.note = (
            f"Đã đối soát: Lê Hải tự động hoàn {abs(transaction.amount):,}đ. "
            f"Khoản hoàn được ghi nhận lúc {refunded_at.isoformat()}."
        )
    logger.info(
        "Matched Le Hai refund: amount=%s audits=%s",
        amount,
        ",".join(str(transaction.id) for transaction in matched),
    )
    return tuple(matched)


async def reconcile_historical_supplier_refunds(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    provider: str = "lehai",
) -> int:
    matched_count = 0
    async with session_factory() as session:
        async with session.begin():
            credits = list(
                await session.scalars(
                    select(SupplierBalanceTransaction)
                    .where(
                        SupplierBalanceTransaction.provider == provider,
                        SupplierBalanceTransaction.kind == "credit",
                        SupplierBalanceTransaction.amount > 0,
                    )
                    .order_by(SupplierBalanceTransaction.id)
                    .with_for_update()
                )
            )
            for credit in credits:
                matched = await match_supplier_refund(
                    session,
                    provider=provider,
                    amount=credit.amount,
                    refunded_at=credit.created_at,
                )
                if not matched:
                    continue
                credit.kind = "refund"
                credit.note = "Lê Hải tự động hoàn tiền cho " + ", ".join(
                    f"Log #{item.id}" for item in matched
                )
                matched_count += len(matched)
    return matched_count


async def reconcile_supplier_balance(
    session_factory: async_sessionmaker[AsyncSession],
    client: ExternalSupplierClient,
    *,
    provider: str = PROVIDER,
    provider_label: str = "Sumi",
) -> SupplierReconcileResult:
    async with supplier_balance_guard(client):
        current_balance = await client.fetch_balance()
        checked_at = datetime.now(UTC)
        async with session_factory() as session:
            async with session.begin():
                state = await session.scalar(
                    select(SupplierBalanceState)
                    .where(SupplierBalanceState.provider == provider)
                    .with_for_update()
                )
                latest_purchase_id = int(
                    await session.scalar(
                        select(func.coalesce(func.max(SupplierBalanceTransaction.id), 0)).where(
                            SupplierBalanceTransaction.provider == provider,
                            SupplierBalanceTransaction.kind == "purchase",
                        )
                    )
                    or 0
                )
                if state is None or state.last_balance is None:
                    if state is None:
                        state = SupplierBalanceState(provider=provider)
                        session.add(state)
                    state.last_balance = current_balance
                    state.last_purchase_id = latest_purchase_id
                    state.checked_at = checked_at
                    return SupplierReconcileResult(current_balance, initialized=True)

                purchases = list(
                    await session.scalars(
                        select(SupplierBalanceTransaction).where(
                            SupplierBalanceTransaction.provider == provider,
                            SupplierBalanceTransaction.kind == "purchase",
                            SupplierBalanceTransaction.id > state.last_purchase_id,
                        )
                    )
                )
                expected_purchase_debit = sum(-transaction.amount for transaction in purchases)
                observed_delta = current_balance - state.last_balance
                unexplained_delta = observed_delta + expected_purchase_debit
                transaction: SupplierBalanceTransaction | None = None
                refunded: tuple[SupplierBalanceTransaction, ...] = ()
                if unexplained_delta < 0:
                    transaction = SupplierBalanceTransaction(
                        provider=provider,
                        kind="suspicious",
                        amount=unexplained_delta,
                        balance_before=state.last_balance,
                        balance_after=current_balance,
                        note=(
                            f"Số dư {provider_label} giảm nhiều hơn tổng chi phí các đơn do shop ghi nhận "
                            "trong cùng kỳ đối soát."
                        ),
                        period_started_at=state.checked_at,
                        created_at=checked_at,
                    )
                    session.add(transaction)
                    await session.flush()
                    logger.warning(
                        "Suspicious %s balance decrease detected: amount=%s before=%s after=%s",
                        provider,
                        unexplained_delta,
                        state.last_balance,
                        current_balance,
                    )
                elif unexplained_delta > 0:
                    refunded = await match_supplier_refund(
                        session,
                        provider=provider,
                        amount=unexplained_delta,
                        refunded_at=checked_at,
                    )
                    session.add(
                        SupplierBalanceTransaction(
                            provider=provider,
                            kind="refund" if refunded else "credit",
                            amount=unexplained_delta,
                            balance_before=state.last_balance,
                            balance_after=current_balance,
                            note=(
                                "Lê Hải tự động hoàn tiền cho "
                                + ", ".join(
                                    f"Log #{item.id}" for item in refunded
                                )
                                if refunded
                                else f"Số dư {provider_label} tăng ngoài các đơn mua của shop."
                            ),
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
                    refunded_amount=unexplained_delta if refunded else 0,
                    refunded_audit_ids=tuple(item.id for item in refunded),
                )
