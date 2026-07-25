import logging
from dataclasses import dataclass

from cryptography.fernet import InvalidToken
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import InventoryDuplicateAlert, InventoryItem
from app.utils import SecretCipher, inventory_account_identity


logger = logging.getLogger(__name__)
INVENTORY_IMPORT_ADVISORY_LOCK = 734220260725


@dataclass(frozen=True)
class AcceptedInventoryItem:
    raw_item: str
    account_fingerprint: str


@dataclass(frozen=True)
class InventoryDuplicateCheck:
    accepted: tuple[AcceptedInventoryItem, ...]
    duplicate_count: int


async def filter_duplicate_inventory(
    session: AsyncSession,
    cipher: SecretCipher,
    *,
    product_id: int,
    raw_items: list[str],
) -> InventoryDuplicateCheck:
    """Serialize Admin imports, keep clean rows and persist every rejected duplicate."""
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(
            text(f"SELECT pg_advisory_xact_lock({INVENTORY_IMPORT_ADVISORY_LOCK})")
        )
    candidates = [
        (
            item,
            inventory_account_identity(item),
            cipher.inventory_fingerprint(item),
        )
        for item in raw_items
    ]
    candidate_fingerprints = {fingerprint for _, _, fingerprint in candidates}
    existing_rows = list(
        await session.scalars(
            select(InventoryItem)
            .where(InventoryItem.account_fingerprint.in_(candidate_fingerprints))
            .order_by(InventoryItem.id.desc())
        )
    )
    existing_by_fingerprint: dict[str, InventoryItem] = {}
    for existing_item in existing_rows:
        if existing_item.account_fingerprint:
            existing_by_fingerprint.setdefault(existing_item.account_fingerprint, existing_item)

    accepted: list[AcceptedInventoryItem] = []
    duplicate_count = 0
    seen_fingerprints: set[str] = set()
    for item, identifier, fingerprint in candidates:
        reason: str | None = None
        existing_item = existing_by_fingerprint.get(fingerprint)
        if fingerprint in seen_fingerprints:
            reason = "duplicate_in_import"
        elif existing_item is not None:
            reason = "duplicate_existing"
        seen_fingerprints.add(fingerprint)
        if reason is None:
            accepted.append(AcceptedInventoryItem(item, fingerprint))
            continue
        duplicate_count += 1
        session.add(
            InventoryDuplicateAlert(
                product_id=product_id,
                existing_inventory_item_id=(
                    existing_item.id if existing_item is not None else None
                ),
                account_fingerprint=fingerprint,
                encrypted_identifier=cipher.encrypt(identifier or "Không xác định"),
                reason=reason,
            )
        )
    return InventoryDuplicateCheck(tuple(accepted), duplicate_count)


async def backfill_inventory_fingerprints(
    session_factory: async_sessionmaker[AsyncSession],
    cipher: SecretCipher,
    *,
    batch_size: int = 250,
) -> int:
    """Backfill stable keyed fingerprints so old sold stock also blocks re-imports."""
    updated = 0
    last_id = 0
    while True:
        async with session_factory() as session:
            items = list(
                await session.scalars(
                    select(InventoryItem)
                    .where(
                        InventoryItem.id > last_id,
                        InventoryItem.account_fingerprint.is_(None),
                    )
                    .order_by(InventoryItem.id)
                    .limit(max(1, batch_size))
                )
            )
            if not items:
                return updated
            for item in items:
                last_id = max(last_id, item.id)
                try:
                    plaintext = cipher.decrypt(item.encrypted_secret)
                except (InvalidToken, UnicodeDecodeError, ValueError):
                    logger.warning("Could not fingerprint inventory item %s", item.id)
                    continue
                item.account_fingerprint = cipher.inventory_fingerprint(plaintext)
                updated += 1
            await session.commit()


async def backfill_historical_duplicate_alerts(
    session_factory: async_sessionmaker[AsyncSession],
    cipher: SecretCipher,
) -> int:
    """Expose duplicate accounts that already existed before fingerprinting was enabled."""
    async with session_factory() as session:
        repeated_fingerprints = list(
            await session.scalars(
                select(InventoryItem.account_fingerprint)
                .where(InventoryItem.account_fingerprint.is_not(None))
                .group_by(InventoryItem.account_fingerprint)
                .having(func.count(InventoryItem.id) > 1)
            )
        )
        if not repeated_fingerprints:
            return 0
        items = list(
            await session.scalars(
                select(InventoryItem)
                .where(InventoryItem.account_fingerprint.in_(repeated_fingerprints))
                .order_by(InventoryItem.account_fingerprint, InventoryItem.id)
            )
        )
        grouped: dict[str, list[InventoryItem]] = {}
        for item in items:
            if item.account_fingerprint:
                grouped.setdefault(item.account_fingerprint, []).append(item)

        inserted = 0
        for fingerprint, duplicate_items in grouped.items():
            original = duplicate_items[0]
            for duplicate in duplicate_items[1:]:
                existing_alert = await session.scalar(
                    select(InventoryDuplicateAlert.id).where(
                        InventoryDuplicateAlert.product_id == duplicate.product_id,
                        InventoryDuplicateAlert.existing_inventory_item_id == original.id,
                        InventoryDuplicateAlert.account_fingerprint == fingerprint,
                        InventoryDuplicateAlert.reason == "historical_duplicate",
                    )
                )
                if existing_alert is not None:
                    continue
                try:
                    plaintext = cipher.decrypt(duplicate.encrypted_secret)
                except (InvalidToken, UnicodeDecodeError, ValueError):
                    logger.warning(
                        "Could not decrypt historical duplicate inventory item %s",
                        duplicate.id,
                    )
                    continue
                session.add(
                    InventoryDuplicateAlert(
                        product_id=duplicate.product_id,
                        existing_inventory_item_id=original.id,
                        account_fingerprint=fingerprint,
                        encrypted_identifier=cipher.encrypt(
                            inventory_account_identity(plaintext) or "Không xác định"
                        ),
                        reason="historical_duplicate",
                    )
                )
                inserted += 1
        await session.commit()
        return inserted
