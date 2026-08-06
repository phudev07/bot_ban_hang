import re
import unicodedata

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    Category,
    Product,
    ProductPriceAlert,
    ProductStockAlert,
    ProductSupplierState,
)


NCE_CATEGORY_VI = "API CODEX & CLAUDE"
NCE_CATEGORY_EN = "CODEX & CLAUDE API"
NCE_CATEGORY_POSITION = 3
NCE_LOCAL_PRODUCT_DEFAULTS = (
    ("codex", 50, 40_000),
    ("codex", 100, 80_000),
    ("codex", 500, 220_000),
    ("claude", 50, 40_000),
    ("claude", 100, 80_000),
)


def _plain_text(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("plain") or value.get("raw") or ""
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def _normalized(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", _plain_text(value))
    return "".join(
        character for character in normalized.lower() if not unicodedata.combining(character)
    )


def nce_product_family(value: object) -> str | None:
    normalized = _normalized(value)
    if "codex" in normalized:
        return "codex"
    if "claude" in normalized:
        return "claude"
    return None


def nce_token_millions(value: object) -> int | None:
    normalized = _normalized(value).replace(",", ".")
    patterns = (
        r"\b(50|100|500)\s*m(?:illion)?\b",
        r"\b(50|100|500)\s*(?:trieu|million)\s*token\b",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            return int(match.group(1))
    return None


def nce_family_from_product(product: Product) -> str | None:
    return nce_product_family(f"{product.name_vi} {product.name_en}")


async def ensure_nce_local_products(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Migrate the old supplier products to encrypted local CDK inventory."""
    async with session_factory() as session:
        category = await session.scalar(
            select(Category).where(Category.name_vi == NCE_CATEGORY_VI)
        )
        if category is None:
            category = Category(
                name_vi=NCE_CATEGORY_VI,
                name_en=NCE_CATEGORY_EN,
                position=NCE_CATEGORY_POSITION,
                active=True,
            )
            session.add(category)
            await session.flush()
        else:
            category.name_en = NCE_CATEGORY_EN
            category.position = NCE_CATEGORY_POSITION
            category.active = True
            category.archived_at = None

        products = list(
            await session.scalars(
                select(Product).where(
                    or_(
                        Product.category_id == category.id,
                        Product.fulfillment_source == "nce",
                    )
                )
            )
        )
        migrated_product_ids: list[int] = []
        for product in products:
            if product.fulfillment_source != "nce":
                continue
            migrated_product_ids.append(product.id)
            product.category_id = category.id
            product.fulfillment_source = "local"
            product.supplier_product_id = None
            product.supplier_markup = 0
            product.supplier_price = None
            product.external_stock = 0
            product.supplier_available_stock = 0
            product.supplier_available_stock_initialized = False
            product.supplier_owner_balance = None
            product.supplier_synced_at = None
            product.price_lock_enabled = False
            product.force_out_of_stock = False
            product.notify_stock_without_balance_topup = False

        if migrated_product_ids:
            await session.execute(
                update(ProductPriceAlert)
                .where(
                    ProductPriceAlert.product_id.in_(migrated_product_ids),
                    ProductPriceAlert.status == "pending",
                )
                .values(status="superseded")
            )
            await session.execute(
                update(ProductStockAlert)
                .where(
                    ProductStockAlert.product_id.in_(migrated_product_ids),
                    ProductStockAlert.status == "pending",
                )
                .values(status="superseded")
            )
            await session.execute(
                delete(ProductSupplierState).where(
                    ProductSupplierState.product_id.in_(migrated_product_ids),
                    ProductSupplierState.provider == "nce",
                )
            )

        if not products:
            for family, token_millions, default_price in NCE_LOCAL_PRODUCT_DEFAULTS:
                family_label = family.upper()
                session.add(
                    Product(
                        category_id=category.id,
                        name_vi=f"API {family_label} - {token_millions}M token",
                        name_en=f"{family_label} API - {token_millions}M tokens",
                        description_vi=(
                            f"Gói API {family_label} {token_millions}M token. "
                            "Mã CDK được giao tự động ngay sau khi thanh toán thành công."
                        ),
                        description_en=(
                            f"{family_label} API package with {token_millions}M tokens. "
                            "The CDK is delivered automatically after successful payment."
                        ),
                        price=default_price,
                        product_type="account",
                        allow_quantity=False,
                        max_quantity=1,
                        fulfillment_source="local",
                        external_stock=0,
                        active=True,
                    )
                )
        await session.commit()
