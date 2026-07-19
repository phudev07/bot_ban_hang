from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Product, ProductStockAlert


async def apply_supplier_stock(
    session: AsyncSession,
    product: Product,
    supplier_available_stock: int,
) -> bool:
    """Store successful supplier stock and queue one alert for a 0-to-positive change."""
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

    if not was_initialized or previous_stock > 0 or new_stock <= 0:
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
