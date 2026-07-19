import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import Category, InventoryItem, Product
from app.price_alerts import apply_supplier_price
from app.stock_alerts import apply_supplier_stock
from app.suppliers import SupplierError, SupplierPurchase, SupplierSnapshot


logger = logging.getLogger(__name__)

LEHAI_PRODUCT_SEEDS: dict[str, dict[str, object]] = {
    "cdk_pixel": {
        "name_vi": "CDK GG Pixel 1Y",
        "name_en": "Google Pro Pixel 1 Year CDK",
        "description_vi": (
            "Bảo hành active. Key đưa tới link ưu đãi, không tự thanh toán. "
            "Vui lòng đọc kỹ hướng dẫn sử dụng trước khi kích hoạt."
        ),
        "description_en": (
            "Activation warranty. The key opens the offer link and does not pay "
            "automatically. Read the activation guide before use."
        ),
        "fallback_price": 25_000,
    },
    "cdk_ggpro_18m": {
        "name_vi": "Link GG Pro Jio 18M",
        "name_en": "Google Pro Jio 18M Link",
        "description_vi": (
            "Bảo hành giữ link 24 giờ. Nhấn link và xác nhận để nâng cấp, không cần thẻ. "
            "Dùng được cho mọi tài khoản và có thể mời thêm 5 thành viên gia đình."
        ),
        "description_en": (
            "The link is guaranteed for 24 hours. Open and confirm to upgrade without a "
            "card. Works with any account and supports five additional family members."
        ),
        "fallback_price": 27_000,
    },
}

CATEGORY_VI = "Gemini / Veo3 / Antigravity"
CATEGORY_EN = "Gemini / Veo3 / Antigravity"


@dataclass(frozen=True)
class LeHaiProduct:
    product_id: str
    name: str
    description: str
    unit_price: int
    stock: int


class LeHaiPremiumClient:
    provider = "lehai"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 15,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.balance_lock = asyncio.Lock()

    @staticmethod
    def _payload_error(payload: object, fallback: str) -> SupplierError:
        if not isinstance(payload, dict):
            return SupplierError(fallback)
        detail = payload.get("detail")
        if isinstance(detail, dict):
            message = str(detail.get("message") or detail.get("error") or detail)
            code = str(detail.get("code") or payload.get("code") or fallback)
        else:
            message = str(
                payload.get("message")
                or payload.get("error")
                or detail
                or fallback
            )
            code = str(payload.get("code") or fallback)
        normalized = f"{code} {message}".lower()
        if "stock" in normalized or "hết hàng" in normalized or "out of stock" in normalized:
            code = "INSUFFICIENT_STOCK"
        elif "balance" in normalized or "wallet" in normalized or "số dư" in normalized:
            code = "INSUFFICIENT_BALANCE"
        return SupplierError(code, message)

    async def _get(self, path: str) -> dict[str, object]:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.get(
                    f"{self.base_url}/{path.lstrip('/')}",
                    params={"key": self.api_key},
                )
        except httpx.HTTPError as exc:
            raise SupplierError("SUPPLIER_UNAVAILABLE", type(exc).__name__) from exc
        return self._decode_response(response)

    async def _post(self, path: str, body: dict[str, object]) -> dict[str, object]:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    f"{self.base_url}/{path.lstrip('/')}",
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise SupplierError("SUPPLIER_UNAVAILABLE", type(exc).__name__) from exc
        return self._decode_response(response)

    @classmethod
    def _decode_response(cls, response: httpx.Response) -> dict[str, object]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise SupplierError("SUPPLIER_INVALID_RESPONSE") from exc
        if response.is_error or not isinstance(payload, dict) or payload.get("success") is False:
            raise cls._payload_error(payload, f"SUPPLIER_HTTP_{response.status_code}")
        return payload

    async def fetch_products(self) -> tuple[LeHaiProduct, ...]:
        payload = await self._get("api/telegram-buyer/products")
        products = payload.get("products")
        if not isinstance(products, list):
            raise SupplierError("SUPPLIER_INVALID_RESPONSE")
        values: list[LeHaiProduct] = []
        for raw_product in products:
            if not isinstance(raw_product, dict):
                continue
            stats = raw_product.get("stats")
            stats_data = stats if isinstance(stats, dict) else {}
            try:
                unit_price = int(
                    raw_product.get("walletPricing")
                    or raw_product.get("pricing")
                    or 0
                )
                stock = int(stats_data.get("available") or 0)
            except (TypeError, ValueError):
                continue
            product_id = str(raw_product.get("_id") or raw_product.get("id") or "").strip()
            if product_id and unit_price > 0:
                values.append(
                    LeHaiProduct(
                        product_id=product_id,
                        name=str(raw_product.get("product_name") or product_id),
                        description=str(raw_product.get("description") or ""),
                        unit_price=unit_price,
                        stock=max(0, stock),
                    )
                )
        return tuple(values)

    async def fetch_balance(self) -> int:
        payload = await self._get("api/telegram-buyer/balance")
        try:
            return int(payload.get("balance") or 0)
        except (TypeError, ValueError) as exc:
            raise SupplierError("SUPPLIER_INVALID_RESPONSE") from exc

    async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
        products, balance = await asyncio.gather(
            self.fetch_products(),
            self.fetch_balance(),
        )
        product = next((item for item in products if item.product_id == product_id), None)
        if product is None:
            raise SupplierError("SUPPLIER_PRODUCT_MISSING")
        return SupplierSnapshot(
            product_id=product.product_id,
            name=product.name,
            description=product.description,
            unit_price=product.unit_price,
            source_stock=product.stock,
            owner_balance=max(0, balance),
        )

    async def buy(
        self,
        product_id: str,
        quantity: int,
        *,
        idempotency_key: str | None = None,
    ) -> SupplierPurchase:
        request_key = idempotency_key or f"shop-{secrets.token_hex(12)}"
        payload = await self._post(
            "api/telegram-buyer/purchase",
            {
                "key": self.api_key,
                "product_id": product_id,
                "quantity": quantity,
                "idempotency_key": request_key,
            },
        )
        delivered = payload.get("deliveredAccounts") or payload.get("delivered_accounts")
        if not isinstance(delivered, list):
            raise SupplierError("SUPPLIER_DELIVERY_INCOMPLETE")
        accounts: list[str] = []
        for item in delivered:
            if isinstance(item, dict):
                raw = str(item.get("raw") or "").strip()
                if not raw:
                    parts = [
                        str(item.get(field) or "").strip()
                        for field in ("user", "password", "verifyEmail", "verify_email")
                    ]
                    raw = " | ".join(dict.fromkeys(part for part in parts if part))
                if raw:
                    accounts.append(raw)
            elif str(item).strip():
                accounts.append(str(item).strip())
        if len(accounts) != quantity:
            raise SupplierError("SUPPLIER_DELIVERY_INCOMPLETE")
        try:
            total_amount = int(payload.get("amount") or payload.get("originalAmount") or 0)
        except (TypeError, ValueError) as exc:
            raise SupplierError("SUPPLIER_INVALID_RESPONSE") from exc
        if total_amount <= 0:
            raise SupplierError("SUPPLIER_INVALID_RESPONSE")
        raw_order_code = str(
            payload.get("orderCode")
            or payload.get("supplierOrderCode")
            or request_key
        ).strip()
        return SupplierPurchase(
            order_code=f"LHP-{raw_order_code}",
            unit_price=total_amount // quantity,
            accounts=tuple(accounts),
            product_id=product_id,
            provider=self.provider,
        )


def create_lehai_client(settings: Settings) -> LeHaiPremiumClient | None:
    api_key = settings.lehai_api_key.get_secret_value()
    if not settings.lehai_enabled or not api_key:
        return None
    return LeHaiPremiumClient(
        settings.lehai_base_url,
        api_key,
        settings.lehai_timeout_seconds,
    )


async def ensure_lehai_products(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    async with session_factory() as session:
        product_ids = tuple(
            product_id
            for product_id in settings.lehai_product_ids
            if product_id in LEHAI_PRODUCT_SEEDS
        )
        configured_ids = set(product_ids) if settings.lehai_enabled else set()
        existing_products = list(
            await session.scalars(
                select(Product).where(Product.fulfillment_source == "lehai")
            )
        )
        for product in existing_products:
            product.active = product.supplier_product_id in configured_ids
            if not product.active:
                product.external_stock = 0

        if not settings.lehai_enabled:
            await session.commit()
            return

        category = await session.scalar(select(Category).where(Category.name_vi == CATEGORY_VI))
        if category is None:
            category = Category(name_vi=CATEGORY_VI, name_en=CATEGORY_EN, position=2)
            session.add(category)
            await session.flush()

        for product_id in product_ids:
            product = next(
                (item for item in existing_products if item.supplier_product_id == product_id),
                None,
            )
            if product is not None:
                continue
            seed = LEHAI_PRODUCT_SEEDS[product_id]
            fallback_price = int(seed["fallback_price"])
            session.add(
                Product(
                    category_id=category.id,
                    name_vi=str(seed["name_vi"]),
                    name_en=str(seed["name_en"]),
                    description_vi=str(seed["description_vi"]),
                    description_en=str(seed["description_en"]),
                    price=fallback_price + settings.lehai_markup,
                    product_type="account",
                    allow_quantity=True,
                    max_quantity=100,
                    fulfillment_source="lehai",
                    supplier_product_id=product_id,
                    supplier_markup=settings.lehai_markup,
                    supplier_price=fallback_price,
                    external_stock=0,
                )
            )
        await session.commit()


async def refresh_lehai_product(
    session: AsyncSession,
    product: Product,
    client: LeHaiPremiumClient | None,
) -> int:
    if product.fulfillment_source != "lehai" or not product.supplier_product_id:
        return product.external_stock
    recovered_stock = int(
        await session.scalar(
            select(func.count(InventoryItem.id)).where(
                InventoryItem.product_id == product.id,
                InventoryItem.status == "available",
            )
        )
        or 0
    )
    if client is None:
        product.external_stock = recovered_stock
        await session.flush()
        return product.external_stock
    try:
        snapshot = await client.fetch_snapshot(product.supplier_product_id)
    except SupplierError as exc:
        product.external_stock = recovered_stock
        await session.flush()
        logger.warning(
            "Le Hai supplier sync failed for product %s: code=%s",
            product.supplier_product_id,
            exc.code,
        )
        return product.external_stock
    product.external_stock = snapshot.effective_stock + recovered_stock
    await apply_supplier_price(session, product, snapshot.unit_price)
    await apply_supplier_stock(session, product, snapshot.effective_stock)
    product.supplier_synced_at = datetime.now(UTC)
    await session.flush()
    return product.external_stock


async def sync_lehai_products(
    session_factory: async_sessionmaker[AsyncSession],
    client: LeHaiPremiumClient | None,
) -> None:
    async with session_factory() as session:
        products = list(
            await session.scalars(
                select(Product).where(
                    Product.fulfillment_source == "lehai",
                    Product.active.is_(True),
                )
            )
        )
        for product in products:
            await refresh_lehai_product(session, product, client)
        await session.commit()
