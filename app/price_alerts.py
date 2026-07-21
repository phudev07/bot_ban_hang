from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.flash_sales import stop_unsafe_flash_sale
from app.models import FlashSaleCampaign, Product, ProductPriceAlert


async def apply_supplier_price(
    session: AsyncSession,
    product: Product,
    supplier_price: int,
) -> bool:
    """Apply a dynamic supplier price and queue one durable alert for a real shop-price drop."""
    if supplier_price <= 0 or product.id is None:
        return False

    locked_product = await session.scalar(
        select(Product)
        .where(Product.id == product.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_product is None:
        return False

    previous_supplier_price = locked_product.supplier_price
    previous_sale_price = int(locked_product.price)
    new_sale_price = supplier_price + max(0, int(locked_product.supplier_markup))
    was_synced = locked_product.supplier_synced_at is not None

    locked_product.supplier_price = supplier_price
    locked_product.price = new_sale_price

    campaigns = list(
        await session.scalars(
            select(FlashSaleCampaign)
            .where(
                FlashSaleCampaign.product_id == locked_product.id,
                or_(
                    FlashSaleCampaign.status == "active",
                    and_(
                        FlashSaleCampaign.status == "completed",
                        FlashSaleCampaign.reserved_quantity > 0,
                    ),
                ),
            )
            .with_for_update()
        )
    )
    for campaign in campaigns:
        stop_unsafe_flash_sale(campaign, locked_product)

    if locked_product.force_out_of_stock:
        await session.execute(
            update(ProductPriceAlert)
            .where(
                ProductPriceAlert.product_id == locked_product.id,
                ProductPriceAlert.status == "pending",
            )
            .values(status="superseded")
        )
        return False

    pending = await session.scalar(
        select(ProductPriceAlert)
        .where(
            ProductPriceAlert.product_id == locked_product.id,
            ProductPriceAlert.status == "pending",
        )
        .order_by(ProductPriceAlert.id.desc())
        .limit(1)
        .with_for_update()
    )
    if pending is not None:
        if new_sale_price < pending.sale_price_before:
            pending.supplier_price_after = supplier_price
            pending.sale_price_after = new_sale_price
        else:
            pending.status = "superseded"
        return False

    if (
        not was_synced
        or previous_supplier_price is None
        or previous_supplier_price <= 0
        or supplier_price >= previous_supplier_price
        or new_sale_price >= previous_sale_price
    ):
        return False

    session.add(
        ProductPriceAlert(
            product_id=locked_product.id,
            provider=locked_product.fulfillment_source,
            supplier_price_before=previous_supplier_price,
            supplier_price_after=supplier_price,
            sale_price_before=previous_sale_price,
            sale_price_after=new_sale_price,
        )
    )
    return True
