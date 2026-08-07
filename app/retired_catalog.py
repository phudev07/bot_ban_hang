from datetime import UTC, datetime

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Category, Product


async def retire_discontinued_api_catalog(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Hide the discontinued API catalog without deleting historical orders."""
    async with session_factory() as session:
        async with session.begin():
            category_ids = list(
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
                        Product.fulfillment_source == "nce",
                        Product.category_id.in_(category_ids),
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
            if category_ids:
                await session.execute(
                    update(Category)
                    .where(Category.id.in_(category_ids))
                    .values(active=False, archived_at=now)
                )
            return max(0, int(result.rowcount or 0))
