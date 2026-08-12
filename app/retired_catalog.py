from datetime import UTC, datetime

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Category, Product


RETIRED_FULFILLMENT_SOURCE = "nce"
CURRENT_CODEX_FULFILLMENT_SOURCE = "haji"


async def retire_discontinued_api_catalog(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Hide the discontinued API catalog without deleting historical orders."""
    async with session_factory() as session:
        async with session.begin():
            legacy_category_ids = list(
                await session.scalars(
                    select(Category.id).where(
                        or_(
                            Category.name_vi.ilike("%codex%claude%"),
                            Category.name_en.ilike("%codex%claude%"),
                        )
                    )
                )
            )
            now = datetime.now(UTC)
            result = await session.execute(
                update(Product)
                .where(
                    or_(
                        Product.fulfillment_source == RETIRED_FULFILLMENT_SOURCE,
                        and_(
                            Product.category_id.in_(legacy_category_ids),
                            Product.fulfillment_source
                            != CURRENT_CODEX_FULFILLMENT_SOURCE,
                        ),
                    ),
                    Product.archived_at.is_(None),
                )
                .values(
                    active=False,
                    archived_at=now,
                    force_out_of_stock=True,
                    external_stock=0,
                    supplier_available_stock=0,
                    sale_notifications_enabled=False,
                    stock_notifications_enabled=False,
                )
            )
            if legacy_category_ids:
                categories_with_live_products = set(
                    await session.scalars(
                        select(Product.category_id).where(
                            Product.category_id.in_(legacy_category_ids),
                            Product.archived_at.is_(None),
                            Product.active.is_(True),
                        )
                    )
                )
                archive_ids = [
                    category_id
                    for category_id in legacy_category_ids
                    if category_id not in categories_with_live_products
                ]
                if archive_ids:
                    await session.execute(
                        update(Category)
                        .where(Category.id.in_(archive_ids))
                        .values(active=False, archived_at=now)
                    )
            return max(0, int(result.rowcount or 0))
