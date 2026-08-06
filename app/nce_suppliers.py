import asyncio
import math
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import Category, InventoryItem, Product
from app.price_alerts import apply_supplier_price
from app.stock_alerts import apply_supplier_stock
from app.suppliers import SupplierError, SupplierPurchase, SupplierSnapshot


NCE_CATEGORY_VI = "API CODEX & CLAUDE"
NCE_CATEGORY_EN = "CODEX & CLAUDE API"
NCE_CATEGORY_POSITION = 3
NCE_SUPPORTED_TOKEN_MILLIONS = (50, 100, 500)
NCE_MARKUPS = {50: 5_000, 100: 10_000, 500: 20_000}


@dataclass(frozen=True)
class NceProduct:
    product_id: str
    family: str
    token_millions: int
    name: str
    description: str
    unit_price: int
    stock: int

    @property
    def markup(self) -> int:
        return NCE_MARKUPS[self.token_millions]

    @property
    def shop_name_vi(self) -> str:
        return f"API {self.family.upper()} - {self.token_millions}M token"

    @property
    def shop_name_en(self) -> str:
        return f"{self.family.upper()} API - {self.token_millions}M tokens"


@dataclass(frozen=True)
class NceOrder:
    order_code: str
    product_id: str
    quantity: int
    status: str
    created_at: datetime | None


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
    if product.fulfillment_source != "nce":
        return None
    return nce_product_family(f"{product.name_vi} {product.name_en}")


def _parse_datetime(value: object) -> datetime | None:
    normalized = str(value or "").strip().replace("Z", "+00:00")
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_int(value: object) -> int:
    try:
        return int(float(str(value or 0)))
    except (TypeError, ValueError):
        return 0


def _delivery_items(payload: dict[str, object]) -> tuple[str, ...]:
    delivered = payload.get("deliveredAccounts") or payload.get("delivered_accounts")
    values: list[str] = []
    if isinstance(delivered, list):
        for item in delivered:
            if isinstance(item, dict):
                raw = _plain_text(item.get("raw"))
                if not raw:
                    parts = [
                        _plain_text(item.get(field))
                        for field in ("user", "password", "key", "code", "secret")
                    ]
                    raw = " | ".join(part for part in parts if part)
                if raw:
                    values.append(raw)
            elif _plain_text(item):
                values.append(_plain_text(item))
    if values:
        return tuple(values)
    delivery_content = _plain_text(
        payload.get("deliveryContent") or payload.get("delivery_content")
    )
    return (delivery_content,) if delivery_content else ()


class NceClient:
    provider = "nce"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        api_prefix: str = "/api/telegram-buyer",
        timeout_seconds: float = 15,
        snapshot_cache_seconds: int = 10,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_prefix = "/" + api_prefix.strip("/")
        self.timeout_seconds = timeout_seconds
        self.snapshot_cache_seconds = max(1, snapshot_cache_seconds)
        self.transport = transport
        self.balance_lock = asyncio.Lock()
        self.refresh_backoff_until: dict[str, float] = {}
        self._http_client: httpx.AsyncClient | None = None
        self._snapshot_lock = asyncio.Lock()
        self._snapshots: dict[str, SupplierSnapshot] = {}
        self._catalog: dict[str, NceProduct] = {}
        self._snapshot_at = 0.0

    def _http(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Accept": "application/json",
                },
                limits=httpx.Limits(
                    max_connections=10,
                    max_keepalive_connections=5,
                    keepalive_expiry=30,
                ),
            )
        return self._http_client

    async def aclose(self) -> None:
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()

    def _url(self, path: str) -> str:
        return f"{self.base_url}{self.api_prefix}/{path.lstrip('/')}"

    @staticmethod
    def _payload_error(response: httpx.Response, payload: object) -> SupplierError:
        data = payload if isinstance(payload, dict) else {}
        message = _plain_text(
            data.get("message") or data.get("error") or data.get("detail")
        )
        normalized = _normalized(message)
        if response.status_code == 404:
            code = "SUPPLIER_PRODUCT_MISSING"
        elif response.status_code == 429:
            code = "SUPPLIER_PURCHASE_BACKOFF"
        elif "stock" in normalized or "het hang" in normalized:
            code = "INSUFFICIENT_STOCK"
        elif "balance" in normalized or "so du" in normalized:
            code = "INSUFFICIENT_BALANCE"
        elif response.status_code in {401, 403}:
            code = "SUPPLIER_AUTH_FAILED"
        elif response.status_code >= 500:
            code = "SUPPLIER_UNAVAILABLE"
        else:
            code = f"SUPPLIER_HTTP_{response.status_code}"
        return SupplierError(code)

    async def _decode(self, response: httpx.Response) -> dict[str, object]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise SupplierError("SUPPLIER_INVALID_RESPONSE") from exc
        if response.is_error or not isinstance(payload, dict) or payload.get("success") is False:
            raise self._payload_error(response, payload)
        return payload

    async def _get(self, path: str) -> dict[str, object]:
        try:
            response = await self._http().get(self._url(path), params={"lang": "vi"})
        except httpx.HTTPError as exc:
            raise SupplierError("SUPPLIER_UNAVAILABLE") from exc
        return await self._decode(response)

    async def _post(
        self,
        path: str,
        body: dict[str, object],
        *,
        idempotency_key: str,
    ) -> dict[str, object]:
        try:
            response = await self._http().post(
                self._url(path),
                params={"lang": "vi"},
                json=body,
                headers={"Idempotency-Key": idempotency_key},
            )
        except httpx.HTTPError as exc:
            raise SupplierError("SUPPLIER_UNAVAILABLE") from exc
        return await self._decode(response)

    async def fetch_products(self) -> tuple[NceProduct, ...]:
        payload = await self._get("products")
        products = payload.get("products")
        if not isinstance(products, list):
            raise SupplierError("SUPPLIER_INVALID_RESPONSE")
        values: list[NceProduct] = []
        for raw_product in products:
            if not isinstance(raw_product, dict):
                continue
            name = _plain_text(raw_product.get("product_name") or raw_product.get("name"))
            family = nce_product_family(name)
            token_millions = nce_token_millions(name)
            if family is None or token_millions not in NCE_SUPPORTED_TOKEN_MILLIONS:
                continue
            product_id = _plain_text(raw_product.get("_id") or raw_product.get("id"))
            unit_price = _safe_int(
                raw_product.get("effectivePricing")
                or raw_product.get("walletPricing")
                or raw_product.get("price")
            )
            stock = _safe_int(raw_product.get("stock"))
            active = raw_product.get("is_active", True) is not False and str(
                raw_product.get("status") or "active"
            ).lower() == "active"
            if not product_id or unit_price <= 0:
                continue
            values.append(
                NceProduct(
                    product_id=product_id,
                    family=family,
                    token_millions=token_millions,
                    name=name,
                    description=_plain_text(raw_product.get("description")),
                    unit_price=unit_price,
                    stock=max(0, stock) if active else 0,
                )
            )
        return tuple(values)

    async def fetch_balance(self) -> int:
        payload = await self._get("balance")
        return max(0, _safe_int(payload.get("balanceVnd") or payload.get("balance")))

    async def refresh_catalog(self, *, force: bool = False) -> tuple[NceProduct, ...]:
        now = time.monotonic()
        if not force and now - self._snapshot_at < self.snapshot_cache_seconds:
            return tuple(self._catalog.values())
        async with self._snapshot_lock:
            now = time.monotonic()
            if not force and now - self._snapshot_at < self.snapshot_cache_seconds:
                return tuple(self._catalog.values())
            products, balance = await asyncio.gather(
                self.fetch_products(),
                self.fetch_balance(),
            )
            self._catalog = {product.product_id: product for product in products}
            self._snapshots = {
                product.product_id: SupplierSnapshot(
                    product_id=product.product_id,
                    name=product.name,
                    description=product.description,
                    unit_price=product.unit_price,
                    source_stock=product.stock,
                    owner_balance=balance,
                )
                for product in products
            }
            self._snapshot_at = now
            return products

    async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
        await self.refresh_catalog()
        snapshot = self._snapshots.get(product_id)
        if snapshot is None:
            raise SupplierError("SUPPLIER_PRODUCT_MISSING")
        return snapshot

    def invalidate_snapshot_cache(self) -> None:
        self._snapshot_at = 0.0

    async def fetch_orders(self) -> tuple[NceOrder, ...]:
        payload = await self._get("orders")
        orders = payload.get("orders")
        if not isinstance(orders, list):
            raise SupplierError("SUPPLIER_INVALID_RESPONSE")
        values: list[NceOrder] = []
        for raw_order in orders:
            if not isinstance(raw_order, dict):
                continue
            order_code = _plain_text(
                raw_order.get("order_code") or raw_order.get("orderCode")
            )
            if not order_code:
                continue
            values.append(
                NceOrder(
                    order_code=order_code,
                    product_id=_plain_text(raw_order.get("product_id")),
                    quantity=max(0, _safe_int(raw_order.get("quantity"))),
                    status=_plain_text(raw_order.get("status")).lower(),
                    created_at=_parse_datetime(raw_order.get("created_at")),
                )
            )
        return tuple(values)

    async def fetch_order(self, order_code: str) -> dict[str, object]:
        payload = await self._get(f"orders/{quote(order_code, safe='')}")
        order = payload.get("order")
        if not isinstance(order, dict):
            raise SupplierError("SUPPLIER_INVALID_RESPONSE")
        return order

    async def _recover_purchase(
        self,
        product_id: str,
        quantity: int,
        known_order_codes: set[str],
    ) -> SupplierPurchase | None:
        try:
            orders = await self.fetch_orders()
        except SupplierError:
            return None
        candidates = [
            order
            for order in orders
            if order.order_code not in known_order_codes
            and order.product_id == product_id
            and order.quantity == quantity
            and order.status in {"completed", "success", "succeeded"}
        ]
        if len(candidates) != 1:
            return None
        order = candidates[0]
        try:
            detail = await self.fetch_order(order.order_code)
        except SupplierError:
            return None
        accounts = _delivery_items(detail)
        total_amount = _safe_int(detail.get("total_amount") or detail.get("amount"))
        if len(accounts) != quantity or total_amount <= 0:
            return None
        return SupplierPurchase(
            order_code=f"NCE-{order.order_code}",
            unit_price=math.ceil(total_amount / quantity),
            accounts=accounts,
            product_id=product_id,
            provider=self.provider,
        )

    async def buy(
        self,
        product_id: str,
        quantity: int,
        *,
        idempotency_key: str | None = None,
    ) -> SupplierPurchase:
        if quantity != 1:
            raise SupplierError("INVALID_QUANTITY")
        request_key = idempotency_key or f"shop-{secrets.token_hex(16)}"
        try:
            known_order_codes: set[str] | None = {
                order.order_code for order in await self.fetch_orders()
            }
        except SupplierError:
            known_order_codes = None
        try:
            payload = await self._post(
                "purchase",
                {"product_id": product_id, "quantity": 1, "coupon_code": ""},
                idempotency_key=request_key,
            )
            accounts = _delivery_items(payload)
            raw_order_code = _plain_text(
                payload.get("orderCode") or payload.get("order_code")
            )
            total_amount = _safe_int(payload.get("amount") or payload.get("total_amount"))
            if not raw_order_code or len(accounts) != 1 or total_amount <= 0:
                raise SupplierError("SUPPLIER_DELIVERY_INCOMPLETE")
            purchase = SupplierPurchase(
                order_code=f"NCE-{raw_order_code}",
                unit_price=total_amount,
                accounts=accounts,
                product_id=product_id,
                provider=self.provider,
            )
        except SupplierError as exc:
            if exc.code not in {
                "SUPPLIER_UNAVAILABLE",
                "SUPPLIER_INVALID_RESPONSE",
                "SUPPLIER_DELIVERY_INCOMPLETE",
            }:
                raise
            recovered = (
                await self._recover_purchase(
                    product_id,
                    quantity,
                    known_order_codes,
                )
                if known_order_codes is not None
                else None
            )
            if recovered is None:
                raise
            purchase = recovered
        self.invalidate_snapshot_cache()
        return purchase


def create_nce_client(settings: Settings) -> NceClient | None:
    api_key = settings.nce_api_key.get_secret_value()
    if not settings.nce_enabled or not api_key:
        return None
    return NceClient(
        settings.nce_base_url,
        api_key,
        api_prefix=settings.nce_api_prefix,
        timeout_seconds=settings.nce_timeout_seconds,
        snapshot_cache_seconds=settings.supplier_ui_cache_seconds,
    )


async def ensure_nce_products(
    session_factory: async_sessionmaker[AsyncSession],
    client: NceClient | None,
) -> None:
    products: tuple[NceProduct, ...] = ()
    if client is not None:
        try:
            products = await client.refresh_catalog(force=True)
        except SupplierError:
            products = ()
    async with session_factory() as session:
        category = await session.scalar(
            select(Category).where(Category.name_vi == NCE_CATEGORY_VI)
        )
        if category is None:
            category = Category(
                name_vi=NCE_CATEGORY_VI,
                name_en=NCE_CATEGORY_EN,
                position=NCE_CATEGORY_POSITION,
                active=client is not None,
            )
            session.add(category)
            await session.flush()
        else:
            category.name_en = NCE_CATEGORY_EN
            category.position = NCE_CATEGORY_POSITION
            category.active = client is not None

        existing = list(
            await session.scalars(
                select(Product).where(Product.fulfillment_source == "nce")
            )
        )
        by_supplier_id = {
            product.supplier_product_id: product
            for product in existing
            if product.supplier_product_id
        }
        live_ids = {product.product_id for product in products}
        for product in existing:
            if product.supplier_product_id not in live_ids:
                product.active = False
                product.external_stock = 0

        for source in products:
            product = by_supplier_id.get(source.product_id)
            description_vi = (
                f"Gói API {source.family.upper()} {source.token_millions}M token. "
                "Mã hoặc key được giao tự động sau khi thanh toán thành công. "
                "Truy cập https://gateway.dichvuright.ai để kích hoạt và kiểm tra quota."
            )
            description_en = (
                f"{source.family.upper()} API package with {source.token_millions}M tokens. "
                "The key is delivered automatically after successful payment."
            )
            if product is None:
                product = Product(
                    category_id=category.id,
                    name_vi=source.shop_name_vi,
                    name_en=source.shop_name_en,
                    description_vi=description_vi,
                    description_en=description_en,
                    price=source.unit_price + source.markup,
                    product_type="account",
                    allow_quantity=False,
                    max_quantity=1,
                    fulfillment_source="nce",
                    supplier_product_id=source.product_id,
                    supplier_markup=source.markup,
                    supplier_price=source.unit_price,
                    external_stock=0,
                    active=True,
                )
                session.add(product)
                continue
            product.category_id = category.id
            product.name_vi = source.shop_name_vi
            product.name_en = source.shop_name_en
            product.description_vi = description_vi
            product.description_en = description_en
            product.product_type = "account"
            product.allow_quantity = False
            product.max_quantity = 1
            product.supplier_markup = source.markup
            product.active = True
        await session.commit()


async def refresh_nce_product(
    session: AsyncSession,
    product: Product,
    client: NceClient | None,
) -> int:
    if product.fulfillment_source != "nce" or not product.supplier_product_id:
        return product.external_stock
    local_stock = int(
        await session.scalar(
            select(func.count(InventoryItem.id))
            .where(
                InventoryItem.product_id == product.id,
                InventoryItem.status == "available",
            )
        )
        or 0
    )
    if client is None:
        product.external_stock = local_stock
        await session.flush()
        return product.external_stock
    try:
        snapshot = await client.fetch_snapshot(product.supplier_product_id)
    except SupplierError:
        # Keep the latest known stock during transient provider outages.
        return max(0, product.external_stock)
    product.supplier_owner_balance = snapshot.owner_balance
    product.external_stock = snapshot.effective_stock + local_stock
    await apply_supplier_price(session, product, snapshot.unit_price, alert_provider="nce")
    await apply_supplier_stock(
        session,
        product,
        snapshot.effective_stock,
        notify_on_increase=product.notify_stock_without_balance_topup,
        local_inventory_stock=local_stock,
        alert_provider="nce",
    )
    product.supplier_synced_at = datetime.now(UTC)
    await session.flush()
    return product.external_stock


async def sync_nce_products(
    session_factory: async_sessionmaker[AsyncSession],
    client: NceClient,
) -> None:
    await ensure_nce_products(session_factory, client)
    async with session_factory() as session:
        products = list(
            await session.scalars(
                select(Product).where(
                    Product.fulfillment_source == "nce",
                    Product.active.is_(True),
                    Product.archived_at.is_(None),
                )
            )
        )
        for product in products:
            await refresh_nce_product(session, product, client)
        await session.commit()
