import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import (
    Category,
    InventoryItem,
    Product,
    SupplierBalanceTransaction,
)
from app.price_alerts import apply_supplier_price
from app.stock_alerts import apply_supplier_stock
from app.suppliers import (
    DEFINITIVE_PRODUCT_UNAVAILABLE_CODES,
    SupplierError,
    SupplierPurchase,
    SupplierSnapshot,
    clear_supplier_refresh_failure,
    mark_supplier_refresh_failure,
    supplier_refresh_is_backed_off,
)


logger = logging.getLogger(__name__)

LEHAI_PRODUCT_SEEDS: dict[str, dict[str, object]] = {
    "cdk_pixel": {
        "category_vi": "Gemini / Veo3 / Antigravity",
        "category_en": "Gemini / Veo3 / Antigravity",
        "category_position": 2,
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
        "category_vi": "Gemini / Veo3 / Antigravity",
        "category_en": "Gemini / Veo3 / Antigravity",
        "category_position": 2,
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
    "gptupi_kbh12k": {
        "category_vi": "🤖Tài Khoản ChatGPT cá nhân",
        "category_en": "ChatGPT",
        "category_position": 1,
        "name_vi": "BHF GPT PLUS GMAIL APPLE PAY",
        "name_en": "BHF ChatGPT Plus Gmail Apple Pay",
        "description_vi": (
            "ChatGPT Plus 30 ngày, thanh toán Apple Pay bằng thẻ Việt, tài khoản Gmail. "
            "Bảo hành đầy đủ trừ trường hợp sử dụng 9Router hoặc VPCS. "
            "Định dạng: Mail GPT | Mật khẩu | Mã 2FA."
        ),
        "description_en": (
            "ChatGPT Plus for 30 days on Gmail, activated with Apple Pay. "
            "Full warranty except when used with 9Router or VPCS. "
            "Format: GPT email | password | 2FA secret."
        ),
        "fallback_price": 130_000,
    },
}

CATEGORY_VI = "Gemini / Veo3 / Antigravity"
CATEGORY_EN = "Gemini / Veo3 / Antigravity"
REFUND_MATCH_WINDOW = timedelta(hours=48)
LEHAI_PRODUCT_ALIASES: dict[str, tuple[str, ...]] = {
    # Lê Hải publishes a temporary product ID during Jio 18M sale campaigns.
    "cdk_ggpro_18m": ("sale_link18mgemini",),
}


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
        snapshot_cache_seconds: int = 5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.snapshot_cache_seconds = max(1, snapshot_cache_seconds)
        self.transport = transport
        self.balance_lock = asyncio.Lock()
        self.refresh_backoff_until: dict[str, float] = {}
        self._http_client: httpx.AsyncClient | None = None
        self._snapshot_lock = asyncio.Lock()
        self._snapshots: dict[str, SupplierSnapshot] = {}
        self._resolved_product_ids: dict[str, str] = {}
        self._snapshot_at = 0.0

    def _http(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
                limits=httpx.Limits(
                    max_connections=20,
                    max_keepalive_connections=10,
                    keepalive_expiry=30,
                ),
            )
        return self._http_client

    async def aclose(self) -> None:
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()

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
            response = await self._http().get(
                f"{self.base_url}/{path.lstrip('/')}",
                params={"key": self.api_key},
            )
        except httpx.HTTPError as exc:
            raise SupplierError("SUPPLIER_UNAVAILABLE", type(exc).__name__) from exc
        return self._decode_response(response)

    async def _post(self, path: str, body: dict[str, object]) -> dict[str, object]:
        try:
            response = await self._http().post(
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
        now = time.monotonic()
        if now - self._snapshot_at < self.snapshot_cache_seconds:
            snapshot = self._snapshots.get(product_id)
            if snapshot is None:
                raise SupplierError("SUPPLIER_PRODUCT_MISSING")
            return snapshot
        async with self._snapshot_lock:
            now = time.monotonic()
            if now - self._snapshot_at >= self.snapshot_cache_seconds:
                products, balance = await asyncio.gather(
                    self.fetch_products(),
                    self.fetch_balance(),
                )
                owner_balance = max(0, balance)
                snapshots = {
                    product.product_id: SupplierSnapshot(
                        product_id=product.product_id,
                        name=product.name,
                        description=product.description,
                        unit_price=product.unit_price,
                        source_stock=product.stock,
                        owner_balance=owner_balance,
                    )
                    for product in products
                }
                resolved_product_ids = {
                    product_id: product_id for product_id in snapshots
                }
                for canonical_id, aliases in LEHAI_PRODUCT_ALIASES.items():
                    resolved_id = next(
                        (
                            candidate_id
                            for candidate_id in (*aliases, canonical_id)
                            if candidate_id in snapshots
                        ),
                        None,
                    )
                    if resolved_id is None:
                        continue
                    resolved_snapshot = snapshots[resolved_id]
                    snapshots[canonical_id] = SupplierSnapshot(
                        product_id=canonical_id,
                        name=resolved_snapshot.name,
                        description=resolved_snapshot.description,
                        unit_price=resolved_snapshot.unit_price,
                        source_stock=resolved_snapshot.source_stock,
                        owner_balance=resolved_snapshot.owner_balance,
                    )
                    resolved_product_ids[canonical_id] = resolved_id
                    if self._resolved_product_ids.get(canonical_id) != resolved_id:
                        logger.info(
                            "Resolved Le Hai product %s to live ID %s",
                            canonical_id,
                            resolved_id,
                        )
                self._snapshots = snapshots
                self._resolved_product_ids = resolved_product_ids
                self._snapshot_at = now
            snapshot = self._snapshots.get(product_id)
            if snapshot is None:
                raise SupplierError("SUPPLIER_PRODUCT_MISSING")
            return snapshot

    def invalidate_snapshot_cache(self) -> None:
        self._snapshot_at = 0.0
        self._snapshots = {}

    async def buy(
        self,
        product_id: str,
        quantity: int,
        *,
        idempotency_key: str | None = None,
    ) -> SupplierPurchase:
        request_key = idempotency_key or f"shop-{secrets.token_hex(12)}"
        resolved_product_id = self._resolved_product_ids.get(product_id, product_id)
        payload = await self._post(
            "api/telegram-buyer/purchase",
            {
                "key": self.api_key,
                "product_id": resolved_product_id,
                "quantity": quantity,
                "idempotency_key": request_key,
            },
        )
        self.invalidate_snapshot_cache()
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


async def _balance_increase_is_api_refund(
    session: AsyncSession,
    balance_before: int,
    balance_after: int,
) -> bool:
    increase = balance_after - balance_before
    if increase <= 0:
        return False

    recorded_refund = await session.scalar(
        select(SupplierBalanceTransaction.id).where(
            SupplierBalanceTransaction.provider == "lehai",
            SupplierBalanceTransaction.kind == "refund",
            SupplierBalanceTransaction.amount == increase,
            SupplierBalanceTransaction.balance_before == balance_before,
            SupplierBalanceTransaction.balance_after == balance_after,
        )
    )
    if recorded_refund is not None:
        return True

    candidates = list(
        await session.scalars(
            select(SupplierBalanceTransaction)
            .where(
                SupplierBalanceTransaction.provider == "lehai",
                SupplierBalanceTransaction.kind == "suspicious",
                SupplierBalanceTransaction.amount < 0,
                SupplierBalanceTransaction.created_at
                >= datetime.now(UTC) - REFUND_MATCH_WINDOW,
                SupplierBalanceTransaction.supplier_order_code.is_(None),
                SupplierBalanceTransaction.shop_order_code.is_(None),
            )
            .order_by(SupplierBalanceTransaction.id.desc())
        )
    )
    if any(abs(transaction.amount) == increase for transaction in candidates):
        return True

    remaining = increase
    for transaction in candidates:
        debit = abs(transaction.amount)
        if debit <= remaining:
            remaining -= debit
        if remaining == 0:
            return True
    return False


def create_lehai_client(settings: Settings) -> LeHaiPremiumClient | None:
    api_key = settings.lehai_api_key.get_secret_value()
    if not settings.lehai_enabled or not api_key:
        return None
    return LeHaiPremiumClient(
        settings.lehai_base_url,
        api_key,
        settings.lehai_timeout_seconds,
        settings.supplier_ui_cache_seconds,
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

        for product_id in product_ids:
            seed = LEHAI_PRODUCT_SEEDS[product_id]
            category_vi = str(seed.get("category_vi") or CATEGORY_VI)
            category_en = str(seed.get("category_en") or category_vi)
            category = await session.scalar(
                select(Category).where(Category.name_vi == category_vi)
            )
            if category is None:
                category = Category(
                    name_vi=category_vi,
                    name_en=category_en,
                    position=int(seed.get("category_position") or 2),
                )
                session.add(category)
                await session.flush()
            product = next(
                (item for item in existing_products if item.supplier_product_id == product_id),
                None,
            )
            if product is not None:
                product.category_id = category.id
                continue
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
        await session.execute(
            delete(Category).where(
                Category.name_vi == "ChatGPT",
                ~Category.products.any(),
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
    if supplier_refresh_is_backed_off(client, product.supplier_product_id):
        product.external_stock = max(0, product.external_stock, recovered_stock)
        return product.external_stock
    try:
        snapshot = await client.fetch_snapshot(product.supplier_product_id)
    except SupplierError as exc:
        # Do not turn a temporary API/network error into a false sold-out
        # state. The purchase path will still ask the provider for the truth.
        product.external_stock = max(0, product.external_stock, recovered_stock)
        definitive = exc.code in DEFINITIVE_PRODUCT_UNAVAILABLE_CODES
        mark_supplier_refresh_failure(
            client,
            product.supplier_product_id,
            definitive=definitive,
        )
        if definitive:
            product.external_stock = recovered_stock
            await apply_supplier_stock(session, product, 0)
            product.supplier_synced_at = datetime.now(UTC)
        await session.flush()
        logger.warning(
            "Le Hai supplier sync failed for product %s: code=%s",
            product.supplier_product_id,
            exc.code,
        )
        return product.external_stock
    clear_supplier_refresh_failure(client, product.supplier_product_id)
    previous_owner_balance = product.supplier_owner_balance
    current_owner_balance = max(0, snapshot.owner_balance)
    balance_increased = (
        previous_owner_balance is not None
        and current_owner_balance > previous_owner_balance
    )
    refund_increase = (
        balance_increased
        and await _balance_increase_is_api_refund(
            session,
            previous_owner_balance,
            current_owner_balance,
        )
    )
    product.supplier_owner_balance = current_owner_balance
    product.external_stock = snapshot.effective_stock + recovered_stock
    await apply_supplier_price(session, product, snapshot.unit_price)
    await apply_supplier_stock(
        session,
        product,
        snapshot.effective_stock,
        notify_on_increase=(
            product.notify_stock_without_balance_topup
            or (balance_increased and not refund_increase)
        ),
    )
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
