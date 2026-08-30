import asyncio
import re
import time
import unicodedata
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


HAJI_PROVIDER = "haji"
HAJI_NETFLIX_CATEGORY_VI = "Netflix"
HAJI_NETFLIX_CATEGORY_EN = "Netflix"
HAJI_NETFLIX_CATEGORY_POSITION = 3
HAJI_CODEX_CATEGORY_VI = "API CODEX"
HAJI_CODEX_CATEGORY_EN = "CODEX API"
HAJI_CODEX_CATEGORY_POSITION = 4
HAJI_CODEX_PRODUCT_MARKUPS = {
    "apicodex_10m_1day": 5_000,
    "apicodex_50m_1day": 15_000,
    "apicodex_100m_1day": 15_000,
    "claude_addteam1x25": 50_000,
}
HAJI_CODEX_PRODUCT_NAMES = {
    "apicodex_10m_1day": ("API Codex 10M Token · 24 giờ", "Codex API 10M Tokens · 24 hours"),
    "apicodex_50m_1day": ("API Codex 50M Token · 24 giờ", "Codex API 50M Tokens · 24 hours"),
    "apicodex_100m_1day": (
        "API Codex 100M Token · 24 giờ",
        "Codex API 100M Tokens · 24 hours",
    ),
}
HAJI_CLAUDE_PRODUCT_NAMES = {
    "claude_addteam1x25": (
        "Claude Team Standard 1 tháng",
        "Claude Team Standard 1 month",
    ),
}
# Only expose the Claude Team slot requested for this shop. Other Claude SKUs
# in the supplier catalog may have different fulfillment or pricing rules.
HAJI_ALLOWED_CLAUDE_PRODUCT_IDS = frozenset({"claude_addteam1x25"})
# Link Gemini 18M is fulfilled through the canonical Le Hai product so the
# customer sees one listing while the route planner can choose the cheapest
# enabled supplier.  Keep the Haji catalog kind for route discovery, but do
# not expose it as a second standalone product.
HAJI_ROUTE_ONLY_PRODUCT_IDS = frozenset({"link_gemini_18moth"})
HAJI_SUPPORTED_KINDS = frozenset(
    {"netflix", "gpt_gcash", "gpt_k12", "codex", "claude", "gemini_18m"}
)


@dataclass(frozen=True)
class HajiProduct:
    product_id: str
    name: str
    description: str
    unit_price: int
    stock: int
    kind: str
    delivery_mode: str = "instant_items"
    requires_emails: bool = False


@dataclass(frozen=True)
class HajiOrderStatus:
    order_code: str
    product_id: str
    quantity: int
    status: str
    unit_price: int
    items: tuple[str, ...]


def _plain_text(value: object) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def _normalized(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", _plain_text(value))
    return "".join(
        character for character in normalized.lower() if not unicodedata.combining(character)
    )


def _safe_int(value: object) -> int:
    try:
        return int(float(str(value or 0)))
    except (TypeError, ValueError):
        return 0


def haji_product_kind(value: object) -> str | None:
    normalized = _normalized(value).replace("_", " ").replace("-", " ")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if "gemini" in normalized and re.search(r"\b18m(?:oth|onth)?\b", normalized):
        return "gemini_18m"
    if "codex" in normalized and re.search(r"\b(?:10|50|100)\s*m\b", normalized):
        return "codex"
    if "claude" in normalized:
        return "claude"
    if "netflix" in normalized:
        return "netflix"
    if ("gpt" in normalized or "chatgpt" in normalized) and "gcash" in normalized:
        return "gpt_gcash"
    k12_marker = re.search(r"\b(?:k12|kbh\s*12|12k)\b", normalized)
    standalone_k12_alias = re.fullmatch(r"(?:kbh\s*12|12k)", normalized)
    if k12_marker and (
        "gpt" in normalized or "chatgpt" in normalized or standalone_k12_alias
    ):
        return "gpt_k12"
    return None


def haji_product_markup(product_id: str, default_markup: int) -> int:
    return HAJI_CODEX_PRODUCT_MARKUPS.get(product_id, max(0, int(default_markup)))


def haji_product_names(source: HajiProduct) -> tuple[str, str]:
    if source.product_id in HAJI_ROUTE_ONLY_PRODUCT_IDS:
        return "Link GG Pro Jio 18M", "Google Pro Jio 18M Link"
    return HAJI_CODEX_PRODUCT_NAMES.get(
        source.product_id,
        HAJI_CLAUDE_PRODUCT_NAMES.get(source.product_id, (source.name, source.name)),
    )


def _delivery_items(data: dict[str, object]) -> tuple[str, ...]:
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        return ()
    values: list[str] = []
    for item in raw_items:
        if isinstance(item, dict):
            value = item.get("value") or item.get("email") or item.get("account")
        else:
            value = item
        text = _plain_text(value)
        if text:
            values.append(text)
    return tuple(values)


class HajiClient:
    provider = HAJI_PROVIDER

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: float = 15,
        snapshot_cache_seconds: int = 10,
        manual_poll_seconds: float = 3.0,
        manual_timeout_seconds: float = 180.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.snapshot_cache_seconds = max(1, snapshot_cache_seconds)
        self.manual_poll_seconds = max(0.2, float(manual_poll_seconds))
        self.manual_timeout_seconds = max(1.0, float(manual_timeout_seconds))
        self.transport = transport
        self.balance_lock = asyncio.Lock()
        self.refresh_backoff_until: dict[str, float] = {}
        self._http_client: httpx.AsyncClient | None = None
        self._snapshot_lock = asyncio.Lock()
        self._snapshots: dict[str, SupplierSnapshot] = {}
        self._catalog: dict[str, HajiProduct] = {}
        self._snapshot_at = 0.0

    def _http(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
                headers={
                    "X-API-Key": self.api_key,
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
        return f"{self.base_url}/{path.lstrip('/')}"

    @staticmethod
    def _payload_error(response: httpx.Response, payload: object) -> SupplierError:
        data = payload if isinstance(payload, dict) else {}
        detail = data.get("detail") or data.get("message") or data.get("error")
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("detail") or detail.get("code")
        message = _plain_text(detail)
        normalized = _normalized(message)
        if response.status_code == 400:
            code = "INVALID_QUANTITY"
        elif response.status_code in {401, 403}:
            code = "SUPPLIER_AUTH_FAILED"
        elif response.status_code == 402:
            code = "INSUFFICIENT_BALANCE"
        elif response.status_code == 404:
            code = "SUPPLIER_PRODUCT_MISSING"
        elif response.status_code == 409 or "het hang" in normalized or "stock" in normalized:
            code = "INSUFFICIENT_STOCK"
        elif response.status_code == 429:
            code = "SUPPLIER_PURCHASE_BACKOFF"
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
        if response.is_error or not isinstance(payload, dict) or payload.get("ok") is not True:
            raise self._payload_error(response, payload)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise SupplierError("SUPPLIER_INVALID_RESPONSE")
        return data

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        try:
            response = await self._http().get(self._url(path), params=params)
        except httpx.HTTPError as exc:
            raise SupplierError("SUPPLIER_UNAVAILABLE") from exc
        return await self._decode(response)

    async def fetch_products(self) -> tuple[HajiProduct, ...]:
        data = await self._get(
            "/api/v2/catalog",
            params={"available_only": "false", "limit": 500, "offset": 0},
        )
        products = data.get("products")
        if not isinstance(products, list):
            raise SupplierError("SUPPLIER_INVALID_RESPONSE")
        values: list[HajiProduct] = []
        for raw_product in products:
            if not isinstance(raw_product, dict):
                continue
            product_id = _plain_text(raw_product.get("product_id"))
            name = _plain_text(raw_product.get("name"))
            kind = haji_product_kind(f"{product_id} {name}")
            unit_price = _safe_int(raw_product.get("price"))
            currency = _plain_text(raw_product.get("currency")).upper()
            if (
                not product_id
                or not name
                or kind not in HAJI_SUPPORTED_KINDS
                or (kind == "claude" and product_id not in HAJI_ALLOWED_CLAUDE_PRODUCT_IDS)
                or unit_price <= 0
                or currency not in {"", "VND"}
            ):
                continue
            available = raw_product.get("available") is not False
            delivery_mode = (
                _plain_text(raw_product.get("delivery_mode")).lower() or "instant_items"
            )
            raw_requires_emails = raw_product.get("requires_emails")
            requires_emails = (
                raw_requires_emails is True
                or _normalized(raw_requires_emails) in {"1", "true", "yes"}
                or delivery_mode == "manual_fulfillment"
            )
            values.append(
                HajiProduct(
                    product_id=product_id,
                    name=name,
                    description=_plain_text(raw_product.get("description")),
                    unit_price=unit_price,
                    stock=(
                        max(0, _safe_int(raw_product.get("stock_count")))
                        if available
                        else 0
                    ),
                    kind=kind,
                    delivery_mode=delivery_mode,
                    requires_emails=requires_emails,
                )
            )
        return tuple(values)

    async def fetch_balance(self) -> int:
        data = await self._get("/api/v2/me")
        return max(0, _safe_int(data.get("balance")))

    async def refresh_catalog(self, *, force: bool = False) -> tuple[HajiProduct, ...]:
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

    async def buy(
        self,
        product_id: str,
        quantity: int,
        *,
        idempotency_key: str | None = None,
        emails: tuple[str, ...] | None = None,
        defer_manual: bool = False,
    ) -> SupplierPurchase:
        if not 1 <= quantity <= 100:
            raise SupplierError("INVALID_QUANTITY")
        request_key = _plain_text(idempotency_key)
        if not request_key:
            raise SupplierError("SUPPLIER_IDEMPOTENCY_REQUIRED")
        product = self._catalog.get(product_id)
        if product is not None and product.requires_emails:
            if emails is None or len(emails) != quantity:
                raise SupplierError("SUPPLIER_EMAIL_REQUIRED")
        try:
            response = await self._http().post(
                self._url("/api/v2/orders"),
                json={
                    "product_id": product_id,
                    "quantity": quantity,
                    "partner_ref": request_key,
                    **({"emails": list(emails)} if emails is not None else {}),
                },
                headers={"x-idempotency-key": request_key},
            )
        except httpx.HTTPError as exc:
            raise SupplierError("SUPPLIER_UNAVAILABLE") from exc
        data = await self._decode(response)
        accounts = _delivery_items(data)
        order_code = _plain_text(data.get("order_code"))
        unit_price = _safe_int(data.get("unit_price"))
        total_price = _safe_int(data.get("total_price"))
        if unit_price <= 0 and total_price > 0:
            unit_price = (total_price + quantity - 1) // quantity
        status = _plain_text(data.get("status")).lower()
        if status in {"cancelled", "canceled", "failed", "refunded"}:
            raise SupplierError("INSUFFICIENT_STOCK")
        if status in {"processing", "pending"}:
            if not order_code:
                raise SupplierError("SUPPLIER_DELIVERY_INCOMPLETE")
            if defer_manual and product is not None and product.requires_emails:
                raise SupplierError(
                    "SUPPLIER_PENDING",
                    supplier_order_code=f"HAJI-{order_code}",
                )
            deadline = time.monotonic() + self.manual_timeout_seconds
            while time.monotonic() < deadline:
                await asyncio.sleep(self.manual_poll_seconds)
                try:
                    current = await self._get(f"/api/v2/orders/{order_code}")
                except SupplierError as exc:
                    if exc.code in {"SUPPLIER_UNAVAILABLE", "SUPPLIER_INVALID_RESPONSE"}:
                        continue
                    raise
                current_status = _plain_text(current.get("status")).lower()
                if current_status in {"cancelled", "canceled", "failed", "refunded"}:
                    raise SupplierError("INSUFFICIENT_STOCK")
                current_items = _delivery_items(current)
                current_unit_price = _safe_int(current.get("unit_price"))
                if current_unit_price > 0:
                    unit_price = current_unit_price
                if current_status in {"done", "fulfilled", "success", "completed"}:
                    accounts = current_items
                    break
            else:
                raise SupplierError(
                    "SUPPLIER_UNAVAILABLE",
                    supplier_order_code=f"HAJI-{order_code}",
                )
        if not order_code or len(accounts) != quantity or unit_price <= 0:
            raise SupplierError("SUPPLIER_DELIVERY_INCOMPLETE")
        self.invalidate_snapshot_cache()
        return SupplierPurchase(
            order_code=f"HAJI-{order_code}",
            unit_price=unit_price,
            accounts=accounts,
            product_id=product_id,
            provider=self.provider,
        )

    async def check_order(self, order_code: str) -> HajiOrderStatus:
        """Read a previously accepted order without submitting a new one."""
        raw_code = _plain_text(order_code)
        if raw_code.startswith("HAJI-"):
            raw_code = raw_code[5:]
        if not raw_code:
            raise SupplierError("SUPPLIER_ORDER_MISSING")
        data = await self._get(f"/api/v2/orders/{raw_code}")
        return HajiOrderStatus(
            order_code=f"HAJI-{raw_code}",
            product_id=_plain_text(data.get("product_id")),
            quantity=max(0, _safe_int(data.get("quantity"))),
            status=_plain_text(data.get("status")).lower(),
            unit_price=max(0, _safe_int(data.get("unit_price"))),
            items=_delivery_items(data),
        )


def create_haji_client(settings: Settings) -> HajiClient | None:
    api_key = settings.haji_api_key.get_secret_value()
    if not settings.haji_enabled or not api_key:
        return None
    return HajiClient(
        settings.haji_base_url,
        api_key,
        timeout_seconds=settings.haji_timeout_seconds,
        snapshot_cache_seconds=settings.supplier_ui_cache_seconds,
    )


def _category_matches(category: Category, *markers: str) -> bool:
    normalized = _normalized(f"{category.name_vi} {category.name_en}")
    return any(marker in normalized for marker in markers)


async def _ensure_categories(session: AsyncSession) -> tuple[Category, Category, Category]:
    categories = list(
        await session.scalars(
            select(Category)
            .where(Category.archived_at.is_(None))
            .order_by(Category.position, Category.id)
        )
    )
    gpt_category = next(
        (
            category
            for category in categories
            if _category_matches(category, "chatgpt", "gpt")
        ),
        None,
    )
    if gpt_category is None:
        gpt_category = Category(
            name_vi="Tài Khoản ChatGPT cá nhân",
            name_en="Personal ChatGPT accounts",
            position=1,
            active=True,
        )
        session.add(gpt_category)
        await session.flush()
    else:
        gpt_category.active = True

    netflix_category = next(
        (category for category in categories if _category_matches(category, "netflix")),
        None,
    )
    if netflix_category is None:
        netflix_category = Category(
            name_vi=HAJI_NETFLIX_CATEGORY_VI,
            name_en=HAJI_NETFLIX_CATEGORY_EN,
            position=HAJI_NETFLIX_CATEGORY_POSITION,
            active=True,
        )
        session.add(netflix_category)
        await session.flush()
    else:
        netflix_category.active = True

    codex_category = next(
        (category for category in categories if _category_matches(category, "api codex")),
        None,
    )
    if codex_category is None:
        codex_category = Category(
            name_vi=HAJI_CODEX_CATEGORY_VI,
            name_en=HAJI_CODEX_CATEGORY_EN,
            position=HAJI_CODEX_CATEGORY_POSITION,
            active=True,
        )
        session.add(codex_category)
        await session.flush()
    else:
        codex_category.name_vi = HAJI_CODEX_CATEGORY_VI
        codex_category.name_en = HAJI_CODEX_CATEGORY_EN
        codex_category.position = HAJI_CODEX_CATEGORY_POSITION
        codex_category.active = True
        codex_category.archived_at = None
    return gpt_category, netflix_category, codex_category


async def ensure_haji_products(
    session_factory: async_sessionmaker[AsyncSession],
    client: HajiClient | None,
    *,
    markup: int,
) -> None:
    products: tuple[HajiProduct, ...] = ()
    catalog_failed = False
    if client is not None:
        try:
            products = await client.refresh_catalog(force=True)
        except SupplierError:
            catalog_failed = True
    async with session_factory() as session:
        existing = list(
            await session.scalars(
                select(Product).where(Product.fulfillment_source == HAJI_PROVIDER)
            )
        )
        if client is None:
            for product in existing:
                if product.supplier_product_id in HAJI_ROUTE_ONLY_PRODUCT_IDS:
                    product.active = False
                product.external_stock = 0
            await session.commit()
            return
        if catalog_failed:
            for product in existing:
                if product.supplier_product_id in HAJI_ROUTE_ONLY_PRODUCT_IDS:
                    product.active = False
                    product.external_stock = 0
            await session.commit()
            return

        gpt_category, netflix_category, codex_category = await _ensure_categories(session)
        by_supplier_id = {
            product.supplier_product_id: product
            for product in existing
            if product.supplier_product_id
        }
        live_ids = {product.product_id for product in products}
        for product in existing:
            if product.supplier_product_id in HAJI_ROUTE_ONLY_PRODUCT_IDS:
                # This catalog item is an alternative route for the canonical
                # Le Hai Gemini listing, never a separate customer product.
                product.active = False
                product.external_stock = 0
                continue
            if product.supplier_product_id not in live_ids:
                product.external_stock = 0
                # Claude is no longer an API catalog item. Hide stale Claude
                # rows that disappeared from the allowed supplier catalog.
                if (product.supplier_product_id or "").startswith("claude_"):
                    product.active = False

        for source in products:
            if source.product_id in HAJI_ROUTE_ONLY_PRODUCT_IDS:
                continue
            category = (
                netflix_category
                if source.kind == "netflix"
                else codex_category
                if source.kind == "codex"
                else gpt_category
            )
            name_vi, name_en = haji_product_names(source)
            if source.kind == "codex":
                description_vi = (
                    "Key kích hoạt gói API Codex, hạn sử dụng 24 giờ tính từ lúc kích hoạt. "
                    "Sau khi nhận key, mở trang hướng dẫn để kích hoạt và kết nối bằng "
                    "9Router hoặc ứng dụng Custom Codex."
                )
                description_en = (
                    "An activation key for a Codex API package valid for 24 hours after activation. "
                    "Open the setup guide after delivery to activate it and connect through "
                    "9Router or Custom Codex."
                )
            elif source.kind == "claude":
                description_vi = (
                    f"{source.name}. Sau khi mua, gửi email Claude cần thêm vào team. "
                    "Đơn sẽ được xử lý tự động và thông báo khi hoàn tất."
                )
                description_en = (
                    f"{source.name}. After purchase, provide the Claude email to add to the team. "
                    "The order is processed automatically and you will be notified when complete."
                )
            else:
                description_vi = (
                    f"{source.name}. Tài khoản được giao tự động ngay sau khi thanh toán thành công."
                )
                description_en = (
                    f"{source.name}. The account is delivered automatically after payment."
                )
            markup_value = haji_product_markup(source.product_id, markup)
            product = by_supplier_id.get(source.product_id)
            if product is None:
                session.add(
                    Product(
                        category_id=category.id,
                        name_vi=name_vi,
                        name_en=name_en,
                        description_vi=description_vi,
                        description_en=description_en,
                        price=source.unit_price + markup_value,
                        product_type="account",
                        allow_quantity=source.kind != "codex",
                        max_quantity=1 if source.kind == "codex" else 100,
                        fulfillment_source=HAJI_PROVIDER,
                        supplier_product_id=source.product_id,
                        supplier_markup=markup_value,
                        supplier_price=source.unit_price,
                        external_stock=0,
                        active=True,
                    )
                )
                continue
            if source.kind == "claude":
                # Claude is a team account product, not an API package. Move
                # existing rows out of the Codex category during sync while
                # retaining the email-based supplier fulfillment flow.
                product.category_id = gpt_category.id
                product.product_type = "account"
                product.allow_quantity = True
                product.max_quantity = 100
            if (
                source.kind == "claude"
                and product.supplier_price is not None
                and int(product.price)
                == int(product.supplier_price) + max(0, int(product.supplier_markup))
            ):
                # Move automatically priced Claude rows to the requested markup;
                # preserve an explicit admin price override.
                product.supplier_markup = markup_value
                product.price = source.unit_price + markup_value
            # Supplier catalog data initializes new rows only. Existing product
            # presentation and visibility belong to the admin and must survive syncs.
        await session.commit()


async def refresh_haji_product(
    session: AsyncSession,
    product: Product,
    client: HajiClient | None,
) -> int:
    if product.fulfillment_source != HAJI_PROVIDER or not product.supplier_product_id:
        return product.external_stock
    local_stock = int(
        await session.scalar(
            select(func.count(InventoryItem.id)).where(
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
        return max(local_stock, product.external_stock)
    product.supplier_owner_balance = snapshot.owner_balance
    product.external_stock = snapshot.effective_stock + local_stock
    await apply_supplier_price(
        session,
        product,
        snapshot.unit_price,
        alert_provider=HAJI_PROVIDER,
    )
    await apply_supplier_stock(
        session,
        product,
        snapshot.effective_stock,
        notify_on_increase=product.notify_stock_without_balance_topup,
        local_inventory_stock=local_stock,
        alert_provider=HAJI_PROVIDER,
    )
    product.supplier_synced_at = datetime.now(UTC)
    await session.flush()
    return product.external_stock


async def sync_haji_products(
    session_factory: async_sessionmaker[AsyncSession],
    client: HajiClient,
    *,
    markup: int,
) -> None:
    await ensure_haji_products(session_factory, client, markup=markup)
    async with session_factory() as session:
        products = list(
            await session.scalars(
                select(Product).where(
                    Product.fulfillment_source == HAJI_PROVIDER,
                    Product.active.is_(True),
                    Product.archived_at.is_(None),
                )
            )
        )
        for product in products:
            await refresh_haji_product(session, product, client)
        await session.commit()
