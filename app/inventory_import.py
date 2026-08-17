"""Shared, transactional inventory import logic for Admin and automation APIs."""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.inventory_dedup import filter_duplicate_inventory
from app.models import (
    InventoryImportNote,
    InventoryItem,
    Product,
    ProductPriceAlert,
)
from app.stock_alerts import queue_inventory_stock_alert
from app.suppliers import EXTERNAL_FULFILLMENT_SOURCES, SELLABLE_FULFILLMENT_SOURCES
from app.utils import SecretCipher, parse_vnd


MAX_INVENTORY_IMPORT_NOTE_LENGTH = 255


@dataclass(frozen=True)
class InventoryImportResult:
    product_id: int
    product_name: str
    accepted_count: int
    duplicate_count: int
    cost_amount: int
    import_note: str | None
    lock_applied: bool
    notification_queued: bool
    stock_before: int
    stock_after: int


class InventoryImportError(ValueError):
    """A user-correctable inventory import validation error."""


def normalize_inventory_items(raw_items: list[str]) -> list[str]:
    normalized: list[str] = []
    for item in raw_items:
        value = str(item).replace("\r\n", "\n").strip()
        if value:
            normalized.append(value)
    return normalized


async def import_inventory(
    session: AsyncSession,
    cipher: SecretCipher,
    *,
    product_id: int,
    raw_items: list[str],
    cost_amount: int | str,
    import_note_id: int | None = None,
    new_import_note: str = "",
    lock_sale_price: bool = False,
    notify_stock_arrival: bool = False,
) -> InventoryImportResult:
    """Import clean inventory rows inside the caller's transaction.

    The caller must commit or roll back the session. Product locking, duplicate
    serialization, stock recalculation, price locking, and stock notification
    queuing intentionally live here so every import surface has identical rules.
    """
    items = normalize_inventory_items(raw_items)
    parsed_cost = parse_vnd(str(cost_amount))
    if parsed_cost is None or parsed_cost < 0:
        raise InventoryImportError("COST_INVALID")
    if not items:
        raise InventoryImportError("ITEMS_EMPTY")

    normalized_new_note = " ".join(str(new_import_note or "").split())
    if len(normalized_new_note) > MAX_INVENTORY_IMPORT_NOTE_LENGTH:
        raise InventoryImportError("IMPORT_NOTE_TOO_LONG")

    product = await session.scalar(
        select(Product).where(Product.id == product_id).with_for_update()
    )
    if (
        product is None
        or product.archived_at is not None
        or product.fulfillment_source not in SELLABLE_FULFILLMENT_SOURCES
        or product.product_type != "account"
    ):
        raise InventoryImportError("PRODUCT_INVALID")
    if not product.active:
        raise InventoryImportError("PRODUCT_HIDDEN")

    duplicate_check = await filter_duplicate_inventory(
        session,
        cipher,
        product_id=product.id,
        raw_items=items,
    )

    import_note = normalized_new_note
    note_record = None
    if import_note:
        note_record = await session.scalar(
            select(InventoryImportNote)
            .where(func.lower(InventoryImportNote.note) == import_note.lower())
            .with_for_update()
        )
        if note_record is None:
            note_record = InventoryImportNote(note=import_note)
            session.add(note_record)
            await session.flush()
        else:
            import_note = note_record.note
    elif import_note_id is not None:
        note_record = await session.get(InventoryImportNote, import_note_id)
        if note_record is None:
            raise InventoryImportError("IMPORT_NOTE_NOT_FOUND")
        import_note = note_record.note
    if note_record is not None:
        note_record.last_used_at = datetime.now(UTC)

    local_stock_before = int(
        await session.scalar(
            select(func.count(InventoryItem.id)).where(
                InventoryItem.product_id == product.id,
                InventoryItem.status == "available",
            )
        )
        or 0
    )
    supplier_stock = (
        max(0, int(product.supplier_available_stock))
        if product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES
        else 0
    )
    total_stock_before = local_stock_before + supplier_stock

    session.add_all(
        [
            InventoryItem(
                product_id=product.id,
                encrypted_secret=cipher.encrypt(candidate.raw_item),
                account_fingerprint=candidate.account_fingerprint,
                cost_amount=parsed_cost,
                import_note=import_note or None,
            )
            for candidate in duplicate_check.accepted
        ]
    )
    await session.flush()

    lock_applied = False
    if lock_sale_price and product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES:
        product.price_lock_enabled = True
        lock_applied = True
        await session.execute(
            update(ProductPriceAlert)
            .where(
                ProductPriceAlert.product_id == product.id,
                ProductPriceAlert.status == "pending",
            )
            .values(status="superseded")
        )

    local_stock_after = int(
        await session.scalar(
            select(func.count(InventoryItem.id)).where(
                InventoryItem.product_id == product.id,
                InventoryItem.status == "available",
            )
        )
        or 0
    )
    total_stock_after = local_stock_after + supplier_stock
    if product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES:
        product.external_stock = total_stock_after

    notification_queued = False
    if notify_stock_arrival and duplicate_check.accepted:
        notification_queued = await queue_inventory_stock_alert(
            session,
            product,
            stock_before=total_stock_before,
            stock_after=total_stock_after,
        )

    return InventoryImportResult(
        product_id=product.id,
        product_name=product.name_vi,
        accepted_count=len(duplicate_check.accepted),
        duplicate_count=duplicate_check.duplicate_count,
        cost_amount=parsed_cost,
        import_note=import_note or None,
        lock_applied=lock_applied,
        notification_queued=notification_queued,
        stock_before=total_stock_before,
        stock_after=total_stock_after,
    )
