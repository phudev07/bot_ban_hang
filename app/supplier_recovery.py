import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    InventoryItem,
    Product,
    SupplierBalanceTransaction,
    SupplierRecoveryRequest,
)
from app.suppliers import (
    SupplierError,
    SupplierOrderSummary,
    SupplierPurchase,
    SumistoreClient,
    supplier_balance_guard,
)
from app.utils import SecretCipher


logger = logging.getLogger(__name__)
RECOVERY_WINDOW = timedelta(hours=24)
SOURCE_CLOCK_SKEW = timedelta(seconds=3)


@dataclass(frozen=True)
class PendingRecoveryResult:
    matched_orders: int = 0
    inserted_accounts: int = 0
    linked_audits: int = 0
    queued_orphans: int = 0


@dataclass(frozen=True)
class OrphanedOrder:
    summary: SupplierOrderSummary
    purchase: SupplierPurchase
    product_id: int


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _request_key(value: str) -> str:
    normalized = value.strip()
    if len(normalized) <= 96:
        return normalized
    return hashlib.sha256(normalized.encode()).hexdigest()


async def queue_supplier_recovery(
    session: AsyncSession,
    *,
    provider: str,
    supplier_product_id: str,
    quantity: int,
    request_key: str,
    started_at: datetime,
    error_code: str,
) -> SupplierRecoveryRequest | None:
    if provider != "sumistore" or quantity < 1:
        return None
    normalized_key = _request_key(request_key)
    existing = await session.scalar(
        select(SupplierRecoveryRequest).where(
            SupplierRecoveryRequest.request_key == normalized_key
        )
    )
    if existing is not None:
        return existing
    product = await session.scalar(
        select(Product).where(
            Product.fulfillment_source == provider,
            Product.supplier_product_id == supplier_product_id,
        )
    )
    if product is None:
        return None
    request = SupplierRecoveryRequest(
        provider=provider,
        request_key=normalized_key,
        product_id=product.id,
        supplier_product_id=supplier_product_id,
        quantity=quantity,
        status="pending",
        error_code=error_code,
        started_at=started_at,
        expires_at=started_at + RECOVERY_WINDOW,
    )
    session.add(request)
    await session.flush()
    return request


async def _known_supplier_order_codes(session: AsyncSession) -> set[str]:
    transaction_codes = await session.scalars(
        select(SupplierBalanceTransaction.supplier_order_code).where(
            SupplierBalanceTransaction.provider == "sumistore",
            SupplierBalanceTransaction.supplier_order_code.is_not(None),
        )
    )
    inventory_codes = await session.scalars(
        select(InventoryItem.supplier_order_code).where(
            InventoryItem.supplier_order_code.is_not(None)
        )
    )
    recovery_codes = await session.scalars(
        select(SupplierRecoveryRequest.supplier_order_code).where(
            SupplierRecoveryRequest.supplier_order_code.is_not(None)
        )
    )
    return {
        code
        for code in (*transaction_codes, *inventory_codes, *recovery_codes)
        if code
    }


async def _store_recovered_order(
    session_factory: async_sessionmaker[AsyncSession],
    cipher: SecretCipher,
    *,
    recovery_id: int,
    supplier_order_code: str,
    supplier_created_at: datetime,
    unit_price: int,
    accounts: tuple[str, ...],
) -> int:
    async with session_factory() as session:
        async with session.begin():
            recovery = await session.scalar(
                select(SupplierRecoveryRequest)
                .where(SupplierRecoveryRequest.id == recovery_id)
                .with_for_update()
            )
            if recovery is None or recovery.status != "pending":
                return 0
            product = await session.get(Product, recovery.product_id)
            if product is None or product.fulfillment_source != "sumistore":
                recovery.status = "invalid_product"
                return 0
            inserted_count = 0
            for item_index, account in enumerate(accounts):
                existing = await session.scalar(
                    select(InventoryItem.id).where(
                        InventoryItem.supplier_order_code == supplier_order_code,
                        InventoryItem.supplier_item_index == item_index,
                    )
                )
                if existing is not None:
                    continue
                session.add(
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt(account),
                        cost_amount=unit_price,
                        supplier_order_code=supplier_order_code,
                        supplier_provider="sumistore",
                        supplier_item_index=item_index,
                        status="available",
                    )
                )
                inserted_count += 1
            recovery.status = "recovered"
            recovery.supplier_order_code = supplier_order_code
            recovery.unit_price = unit_price
            recovery.total_cost = unit_price * len(accounts)
            recovery.inserted_count = inserted_count
            recovery.supplier_created_at = supplier_created_at
            recovery.recovered_at = datetime.now(UTC)
            return inserted_count


async def _link_recovered_audits(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    linked = 0
    async with session_factory() as session:
        async with session.begin():
            audits = list(
                await session.scalars(
                    select(SupplierBalanceTransaction)
                    .where(
                        SupplierBalanceTransaction.provider == "sumistore",
                        SupplierBalanceTransaction.kind == "suspicious",
                    )
                    .order_by(SupplierBalanceTransaction.id)
                    .with_for_update()
                )
            )
            recoveries = list(
                await session.scalars(
                    select(SupplierRecoveryRequest)
                    .where(
                        SupplierRecoveryRequest.provider == "sumistore",
                        SupplierRecoveryRequest.status == "recovered",
                        SupplierRecoveryRequest.audit_transaction_id.is_(None),
                        SupplierRecoveryRequest.supplier_created_at.is_not(None),
                    )
                    .order_by(SupplierRecoveryRequest.supplier_created_at)
                    .with_for_update()
                )
            )
            for audit in audits:
                if audit.period_started_at is None or audit.created_at is None:
                    continue
                period_started_at = _as_utc(audit.period_started_at)
                audit_created_at = _as_utc(audit.created_at)
                candidates = [
                    recovery
                    for recovery in recoveries
                    if recovery.supplier_created_at is not None
                    and period_started_at - SOURCE_CLOCK_SKEW
                    <= _as_utc(recovery.supplier_created_at)
                    <= audit_created_at + SOURCE_CLOCK_SKEW
                ]
                if sum(recovery.total_cost for recovery in candidates) != abs(audit.amount):
                    continue
                order_codes = [
                    recovery.supplier_order_code
                    for recovery in candidates
                    if recovery.supplier_order_code
                ]
                product_ids = {recovery.product_id for recovery in candidates}
                audit.kind = "recovered"
                audit.supplier_order_code = order_codes[0] if len(order_codes) == 1 else None
                audit.product_id = next(iter(product_ids)) if len(product_ids) == 1 else None
                audit.quantity = sum(recovery.quantity for recovery in candidates)
                audit.note = (
                    f"Đã tự động thu hồi {audit.quantity} tài khoản từ "
                    f"{len(order_codes)} đơn Sumi hoàn tất muộn sau timeout. "
                    "Hàng đã được mã hóa và nhập lại kho."
                )
                for recovery in candidates:
                    recovery.audit_transaction_id = audit.id
                    recoveries.remove(recovery)
                linked += 1
    return linked


async def _queue_orphaned_audit_orders(
    session_factory: async_sessionmaker[AsyncSession],
    client: SumistoreClient,
    summaries: list[SupplierOrderSummary],
    known_codes: set[str],
    now: datetime,
) -> tuple[int, ...]:
    async with session_factory() as session:
        audits = list(
            await session.scalars(
                select(SupplierBalanceTransaction)
                .where(
                    SupplierBalanceTransaction.provider == "sumistore",
                    SupplierBalanceTransaction.kind == "suspicious",
                    SupplierBalanceTransaction.created_at >= now - RECOVERY_WINDOW,
                )
                .order_by(SupplierBalanceTransaction.id)
            )
        )
        products = {
            product.supplier_product_id: product
            for product in await session.scalars(
                select(Product).where(
                    Product.fulfillment_source == "sumistore",
                    Product.supplier_product_id.is_not(None),
                )
            )
            if product.supplier_product_id
        }

    queued_ids: list[int] = []
    purchase_cache: dict[str, SupplierPurchase] = {}
    for audit in audits:
        if audit.period_started_at is None or audit.created_at is None:
            continue
        period_started_at = _as_utc(audit.period_started_at)
        audit_created_at = _as_utc(audit.created_at)
        source_candidates = [
            summary
            for summary in summaries
            if summary.order_code not in known_codes
            and summary.product_id in products
            and period_started_at - SOURCE_CLOCK_SKEW
            <= summary.created_at
            <= audit_created_at + SOURCE_CLOCK_SKEW
        ]
        if not source_candidates:
            continue

        candidates: list[OrphanedOrder] = []
        invalid_candidate = False
        for summary in source_candidates:
            try:
                purchase = purchase_cache.get(summary.order_code)
                if purchase is None:
                    purchase = await client.fetch_order(summary.order_code)
                    purchase_cache[summary.order_code] = purchase
            except SupplierError:
                invalid_candidate = True
                break
            if (
                (
                    purchase.product_id
                    and purchase.product_id != summary.product_id
                )
                or len(purchase.accounts) != summary.quantity
                or purchase.unit_price <= 0
            ):
                invalid_candidate = True
                break
            candidates.append(
                OrphanedOrder(
                    summary=summary,
                    purchase=purchase,
                    product_id=products[summary.product_id].id,
                )
            )
        if invalid_candidate or not candidates:
            continue
        if (
            sum(
                candidate.purchase.unit_price * len(candidate.purchase.accounts)
                for candidate in candidates
            )
            != abs(audit.amount)
        ):
            continue

        async with session_factory() as session:
            async with session.begin():
                locked_audit = await session.scalar(
                    select(SupplierBalanceTransaction)
                    .where(SupplierBalanceTransaction.id == audit.id)
                    .with_for_update()
                )
                if locked_audit is None or locked_audit.kind != "suspicious":
                    continue
                current_known_codes = await _known_supplier_order_codes(session)
                if any(
                    candidate.summary.order_code in current_known_codes
                    for candidate in candidates
                ):
                    continue
                for candidate in candidates:
                    request = SupplierRecoveryRequest(
                        provider="sumistore",
                        request_key=_request_key(
                            f"audit-{audit.id}-{candidate.summary.order_code}"
                        ),
                        product_id=candidate.product_id,
                        supplier_product_id=candidate.summary.product_id,
                        quantity=candidate.summary.quantity,
                        status="pending",
                        error_code="MISSING_LOCAL_COMMIT",
                        supplier_order_code=candidate.summary.order_code,
                        unit_price=candidate.purchase.unit_price,
                        total_cost=(
                            candidate.purchase.unit_price
                            * len(candidate.purchase.accounts)
                        ),
                        started_at=candidate.summary.created_at,
                        expires_at=now + RECOVERY_WINDOW,
                        supplier_created_at=candidate.summary.created_at,
                    )
                    session.add(request)
                    await session.flush()
                    queued_ids.append(request.id)
                    known_codes.add(candidate.summary.order_code)
    return tuple(queued_ids)


async def _recover_requests(
    session_factory: async_sessionmaker[AsyncSession],
    client: SumistoreClient,
    cipher: SecretCipher,
    recoveries: list[SupplierRecoveryRequest],
    summaries: list[SupplierOrderSummary],
    known_codes: set[str],
) -> tuple[int, int]:
    matched_orders = 0
    inserted_accounts = 0
    for recovery in recoveries:
        started_at = _as_utc(recovery.started_at)
        expires_at = _as_utc(recovery.expires_at)
        if recovery.supplier_order_code:
            candidate = next(
                (
                    order
                    for order in summaries
                    if order.order_code == recovery.supplier_order_code
                ),
                None,
            )
        else:
            candidate = next(
                (
                    order
                    for order in summaries
                    if order.order_code not in known_codes
                    and order.product_id == recovery.supplier_product_id
                    and order.quantity == recovery.quantity
                    and started_at - SOURCE_CLOCK_SKEW
                    <= order.created_at
                    <= expires_at
                ),
                None,
            )
        if candidate is None:
            continue
        purchase = await client.fetch_order(candidate.order_code)
        if purchase.product_id and purchase.product_id != recovery.supplier_product_id:
            continue
        if len(purchase.accounts) != recovery.quantity:
            continue
        if purchase.unit_price <= 0:
            continue
        inserted_accounts += await _store_recovered_order(
            session_factory,
            cipher,
            recovery_id=recovery.id,
            supplier_order_code=candidate.order_code,
            supplier_created_at=candidate.created_at,
            unit_price=purchase.unit_price,
            accounts=purchase.accounts,
        )
        known_codes.add(candidate.order_code)
        matched_orders += 1
    return matched_orders, inserted_accounts


async def recover_pending_sumistore_orders(
    session_factory: async_sessionmaker[AsyncSession],
    client: SumistoreClient,
    cipher: SecretCipher,
) -> PendingRecoveryResult:
    now = datetime.now(UTC)
    async with supplier_balance_guard(client):
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(SupplierRecoveryRequest)
                    .where(
                        SupplierRecoveryRequest.provider == "sumistore",
                        SupplierRecoveryRequest.status == "pending",
                        SupplierRecoveryRequest.expires_at < now,
                    )
                    .values(status="expired")
                )
                pending = list(
                    await session.scalars(
                        select(SupplierRecoveryRequest)
                        .where(
                            SupplierRecoveryRequest.provider == "sumistore",
                            SupplierRecoveryRequest.status == "pending",
                            SupplierRecoveryRequest.expires_at >= now,
                        )
                        .order_by(SupplierRecoveryRequest.started_at)
                    )
                )
                known_codes = await _known_supplier_order_codes(session)
        has_suspicious = False
        async with session_factory() as session:
            has_suspicious = bool(
                await session.scalar(
                    select(SupplierBalanceTransaction.id)
                    .where(
                        SupplierBalanceTransaction.provider == "sumistore",
                        SupplierBalanceTransaction.kind == "suspicious",
                        SupplierBalanceTransaction.created_at >= now - RECOVERY_WINDOW,
                    )
                    .limit(1)
                )
            )
        if not pending and not has_suspicious:
            return PendingRecoveryResult()

        summaries = sorted(await client.fetch_orders(), key=lambda item: item.created_at)
        matched_orders, inserted_accounts = await _recover_requests(
            session_factory,
            client,
            cipher,
            pending,
            summaries,
            known_codes,
        )
        linked = await _link_recovered_audits(session_factory)
        queued_ids = await _queue_orphaned_audit_orders(
            session_factory,
            client,
            summaries,
            known_codes,
            now,
        )
        if queued_ids:
            async with session_factory() as session:
                orphan_recoveries = list(
                    await session.scalars(
                        select(SupplierRecoveryRequest)
                        .where(SupplierRecoveryRequest.id.in_(queued_ids))
                        .order_by(SupplierRecoveryRequest.id)
                    )
                )
            orphan_matched, orphan_inserted = await _recover_requests(
                session_factory,
                client,
                cipher,
                orphan_recoveries,
                summaries,
                known_codes,
            )
            matched_orders += orphan_matched
            inserted_accounts += orphan_inserted
            linked += await _link_recovered_audits(session_factory)

    return PendingRecoveryResult(
        matched_orders=matched_orders,
        inserted_accounts=inserted_accounts,
        linked_audits=linked,
        queued_orphans=len(queued_ids),
    )
