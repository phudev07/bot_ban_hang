from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Product, ProductStockAlert


# Only these three shop products are advertised as automatic "back in stock"
# notifications. Other products remain sellable but stay quiet in Telegram.
STOCK_ALERT_PRODUCT_IDS = frozenset(
    {
        "SP-GEF55PBV",  # GPT Plus
        "cdk_pixel",  # CDK GG Pixel 1Y
        "cdk_ggpro_18m",  # Link GG Pro Jio 18M
    }
)


def stock_alert_enabled(product: Product) -> bool:
    return product.supplier_product_id in STOCK_ALERT_PRODUCT_IDS


async def apply_supplier_stock(
    session: AsyncSession,
    product: Product,
    supplier_available_stock: int,
) -> bool:
    """Store successful supplier stock and queue one alert whenever stock increases."""
    if product.id is None:
        return False

    locked_product = await session.scalar(
        select(Product)
        .where(Product.id == product.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_product is None:
        return False

    new_stock = max(0, int(supplier_available_stock))
    previous_stock = max(0, int(locked_product.supplier_available_stock))
    was_initialized = locked_product.supplier_available_stock_initialized
    locked_product.supplier_available_stock = new_stock
    locked_product.supplier_available_stock_initialized = True

    if not stock_alert_enabled(locked_product):
        await session.execute(
            update(ProductStockAlert)
            .where(
                ProductStockAlert.product_id == locked_product.id,
                ProductStockAlert.status == "pending",
            )
            .values(status="superseded")
        )
        return False

    pending = await session.scalar(
        select(ProductStockAlert)
        .where(
            ProductStockAlert.product_id == locked_product.id,
            ProductStockAlert.status == "pending",
        )
        .order_by(ProductStockAlert.id.desc())
        .limit(1)
        .with_for_update()
    )
    if pending is not None:
        if new_stock > 0:
            pending.stock_after = new_stock
            pending.sale_price = locked_product.price
        else:
            pending.status = "superseded"
        return False

    if not was_initialized or new_stock <= previous_stock:
        return False

    session.add(
        ProductStockAlert(
            product_id=locked_product.id,
            provider=locked_product.fulfillment_source,
            stock_before=previous_stock,
            stock_after=new_stock,
            sale_price=locked_product.price,
        )
    )
    return True
