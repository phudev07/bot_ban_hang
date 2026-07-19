import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import Category, InventoryItem, Product
from app.price_alerts import apply_supplier_price


logger = logging.getLogger(__name__)

EXTERNAL_FULFILLMENT_SOURCES = ("sumistore", "lehai")
SELLABLE_FULFILLMENT_SOURCES = ("local", *EXTERNAL_FULFILLMENT_SOURCES)


SUMISTORE_PRODUCT_SEEDS: dict[str, dict[str, object]] = {
    "SP-GEF55PBV": {
        "category_vi": "ChatGPT",
        "category_en": "ChatGPT",
        "name_vi": "GPT PLUS HÀNG MAIL ICLOUD UPI",
        "name_en": "ChatGPT Plus iCloud mail account",
        "fallback_price": 15_000,
    },
    "SP-JMYJL2PL": {
        "category_vi": "ChatGPT",
        "category_en": "ChatGPT",
        "name_vi": "CHAT GPT TRẮNG MAIL ICLOUD",
        "name_en": "New ChatGPT iCloud mail account",
        "fallback_price": 1_000,
    },
}


class SupplierError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class SupplierSnapshot:
    product_id: str
    name: str
    description: str
    unit_price: int
    source_stock: int
    owner_balance: int

    @property
    def effective_stock(self) -> int:
        if self.unit_price <= 0:
            return 0
        return max(0, min(self.source_stock, self.owner_balance // self.unit_price))


@dataclass(frozen=True)
class SupplierPurchase:
    order_code: str
    unit_price: int
    accounts: tuple[str, ...]
    product_id: str = ""
    provider: str = "sumistore"


@dataclass(frozen=True)
class SupplierOrderSummary:
    order_code: str
    product_id: str
    quantity: int
    created_at: datetime


class ExternalSupplierClient(Protocol):
    provider: str
    balance_lock: asyncio.Lock

    async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot: ...

    async def fetch_balance(self) -> int: ...

    async def buy(
        self,
        product_id: str,
        quantity: int,
        *,
        idempotency_key: str | None = None,
    ) -> SupplierPurchase: ...


def _parse_supplier_datetime(value: object) -> datetime | None:
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


class SumistoreClient:
    provider = "sumistore"

    def __init__(
        self,
        base_url: str,
        api_id: str,
        timeout_seconds: float = 15,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_id = api_id
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.balance_lock = asyncio.Lock()

    def _headers(self) -> dict[str, str]:
        return {"X-Tele-API-ID": self.api_id}

    @staticmethod
    def _payload_error(payload: object, fallback: str) -> SupplierError:
        if isinstance(payload, dict):
            return SupplierError(
                str(payload.get("code") or fallback),
                str(payload.get("message") or payload.get("error") or fallback),
            )
        return SupplierError(fallback)

    async def _get(self, path: str) -> dict[str, object]:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.get(
                    f"{self.base_url}/{path.lstrip('/')}",
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise SupplierError("SUPPLIER_UNAVAILABLE", str(exc)) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise SupplierError("SUPPLIER_INVALID_RESPONSE") from exc
        if response.is_error or not isinstance(payload, dict) or payload.get("success") is False:
            raise self._payload_error(payload, f"SUPPLIER_HTTP_{response.status_code}")
        return payload

    async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
        product_payload, balance_payload = await asyncio.gather(
            self._get(f"tele-products/{product_id}"),
            self._get("tele-balance"),
        )
        product_data = product_payload.get("product")
        if not isinstance(product_data, dict):
            products = product_payload.get("products")
            if isinstance(products, list) and products and isinstance(products[0], dict):
                product_data = products[0]
        if not isinstance(product_data, dict):
            raise SupplierError("SUPPLIER_PRODUCT_MISSING")

        owner = balance_payload.get("owner")
        if not isinstance(owner, dict):
            owner = balance_payload
        try:
            unit_price = int(product_data.get("price") or 0)
            source_stock = int(product_data.get("stock") or 0)
            owner_balance = int(owner.get("balance") or 0)
        except (TypeError, ValueError) as exc:
            raise SupplierError("SUPPLIER_INVALID_RESPONSE") from exc
        return SupplierSnapshot(
            product_id=str(product_data.get("id") or product_id),
            name=str(product_data.get("name") or product_id),
            description=str(product_data.get("description") or ""),
            unit_price=unit_price,
            source_stock=source_stock,
            owner_balance=owner_balance,
        )

    async def fetch_balance(self) -> int:
        payload = await self._get("tele-balance")
        owner = payload.get("owner")
        if not isinstance(owner, dict):
            owner = payload
        try:
            return int(owner.get("balance") or 0)
        except (TypeError, ValueError) as exc:
            raise SupplierError("SUPPLIER_INVALID_RESPONSE") from exc

    async def buy(
        self,
        product_id: str,
        quantity: int,
        *,
        idempotency_key: str | None = None,
    ) -> SupplierPurchase:
        body = json.dumps(
            {"id": product_id, "quantity": quantity},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        signature_payload = f"{timestamp}|{nonce}|{body}".encode()
        signature = hmac.new(
            self.api_id.encode(),
            signature_payload,
            hashlib.sha256,
        ).hexdigest()
        headers = {
            **self._headers(),
            "Content-Type": "application/json",
            "X-Timestamp": timestamp,
            "X-Nonce": nonce,
            "X-Signature": signature,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    f"{self.base_url}/tele-product/buy",
                    headers=headers,
                    content=body.encode(),
                )
        except httpx.HTTPError as exc:
            raise SupplierError("SUPPLIER_UNAVAILABLE", str(exc)) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise SupplierError("SUPPLIER_INVALID_RESPONSE") from exc
        if response.is_error or not isinstance(payload, dict) or payload.get("success") is False:
            raise self._payload_error(payload, f"SUPPLIER_HTTP_{response.status_code}")

        accounts = _extract_accounts(payload)
        if len(accounts) != quantity:
            raise SupplierError("SUPPLIER_DELIVERY_INCOMPLETE")
        pricing = payload.get("pricing")
        unit_price = int(pricing.get("unit_price") or 0) if isinstance(pricing, dict) else 0
        return SupplierPurchase(
            order_code=str(payload.get("order_code") or ""),
            unit_price=unit_price,
            accounts=tuple(accounts),
            product_id=product_id,
            provider=self.provider,
        )

    async def fetch_orders(self) -> tuple[SupplierOrderSummary, ...]:
        payload = await self._get("tele-orders")
        orders = payload.get("orders")
        if not isinstance(orders, list):
            raise SupplierError("SUPPLIER_INVALID_RESPONSE")
        summaries: list[SupplierOrderSummary] = []
        for raw_order in orders:
            if not isinstance(raw_order, dict):
                continue
            product = raw_order.get("product")
            product_data = product if isinstance(product, dict) else {}
            created_at = _parse_supplier_datetime(raw_order.get("created_at"))
            try:
                quantity = int(raw_order.get("quantity") or 0)
            except (TypeError, ValueError):
                continue
            order_code = str(raw_order.get("order_code") or "").strip()
            product_id = str(
                product_data.get("id") or raw_order.get("product_id") or ""
            ).strip()
            if order_code and product_id and quantity > 0 and created_at is not None:
                summaries.append(
                    SupplierOrderSummary(
                        order_code=order_code,
                        product_id=product_id,
                        quantity=quantity,
                        created_at=created_at,
                    )
                )
        return tuple(summaries)

    async def fetch_order(self, order_code: str) -> SupplierPurchase:
        payload = await self._get(f"tele-orders/{order_code}")
        raw_order = payload.get("order")
        order = raw_order if isinstance(raw_order, dict) else payload
        accounts = _extract_accounts(order)
        try:
            quantity = int(order.get("quantity") or len(accounts))
        except (TypeError, ValueError) as exc:
            raise SupplierError("SUPPLIER_INVALID_RESPONSE") from exc
        if quantity < 1 or len(accounts) != quantity:
            raise SupplierError("SUPPLIER_DELIVERY_INCOMPLETE")
        pricing = order.get("pricing")
        pricing_data = pricing if isinstance(pricing, dict) else {}
        try:
            total_amount = int(order.get("total_amount") or order.get("amount") or 0)
            unit_price = int(pricing_data.get("unit_price") or 0)
        except (TypeError, ValueError) as exc:
            raise SupplierError("SUPPLIER_INVALID_RESPONSE") from exc
        if unit_price <= 0 and total_amount > 0:
            unit_price = total_amount // quantity
        product = order.get("product")
        product_data = product if isinstance(product, dict) else {}
        return SupplierPurchase(
            order_code=str(order.get("order_code") or order_code),
            unit_price=max(0, unit_price),
            accounts=tuple(accounts),
            product_id=str(product_data.get("id") or order.get("product_id") or ""),
            provider=self.provider,
        )

    async def recover_recent_purchase(
        self,
        product_id: str,
        quantity: int,
        *,
        started_at: datetime,
        known_order_codes: set[str],
    ) -> SupplierPurchase | None:
        earliest = started_at.astimezone(UTC) - timedelta(seconds=3)
        for attempt in range(3):
            summaries = await self.fetch_orders()
            candidates = [
                order
                for order in summaries
                if order.order_code not in known_order_codes
                and order.product_id == product_id
                and order.quantity == quantity
                and order.created_at >= earliest
            ]
            if len(candidates) == 1:
                return await self.fetch_order(candidates[0].order_code)
            if len(candidates) > 1:
                return None
            if attempt < 2:
                await asyncio.sleep(0.5)
        return None


@asynccontextmanager
async def supplier_balance_guard(client: ExternalSupplierClient) -> AsyncIterator[None]:
    lock = getattr(client, "balance_lock", None)
    if lock is None:
        yield
        return
    async with lock:
        yield


def _extract_accounts(payload: dict[str, object]) -> list[str]:
    raw_accounts = payload.get("raw_accounts")
    if isinstance(raw_accounts, list):
        values = [str(value).strip() for value in raw_accounts if str(value).strip()]
        if values:
            return values
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        return []
    values = []
    for account in accounts:
        if isinstance(account, dict):
            raw = str(account.get("raw") or "").strip()
            if not raw:
                email = str(account.get("email") or "").strip()
                password = str(account.get("password") or "").strip()
                raw = "|".join(value for value in (email, password) if value)
            if raw:
                values.append(raw)
        elif str(account).strip():
            values.append(str(account).strip())
    return values


def create_sumistore_client(settings: Settings) -> SumistoreClient | None:
    api_id = settings.sumistore_api_id.get_secret_value()
    if not settings.sumistore_enabled or not api_id:
        return None
    return SumistoreClient(
        settings.sumistore_base_url,
        api_id,
        settings.sumistore_timeout_seconds,
    )


async def ensure_sumistore_product(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    async with session_factory() as session:
        product_ids = tuple(
            supplier_product_id
            for supplier_product_id in settings.sumistore_product_ids
            if supplier_product_id in SUMISTORE_PRODUCT_SEEDS
        )
        configured_ids = set(product_ids)
        existing_products = list(
            await session.scalars(
                select(Product).where(Product.fulfillment_source == "sumistore")
            )
        )
        for product in existing_products:
            if product.supplier_product_id not in configured_ids:
                product.active = False
                product.external_stock = 0

        for position, supplier_product_id in enumerate(
            product_ids,
            start=1,
        ):
            product = await session.scalar(
                select(Product).where(
                    Product.fulfillment_source == "sumistore",
                    Product.supplier_product_id == supplier_product_id,
                )
            )
            if product is not None:
                if not settings.sumistore_enabled:
                    product.external_stock = 0
                continue

            seed = SUMISTORE_PRODUCT_SEEDS.get(supplier_product_id, {})
            category_vi = str(seed.get("category_vi") or "Sumistore")
            category_en = str(seed.get("category_en") or category_vi)
            category = await session.scalar(
                select(Category).where(Category.name_vi == category_vi)
            )
            if category is None:
                category = Category(
                    name_vi=category_vi,
                    name_en=category_en,
                    position=position,
                )
                session.add(category)
                await session.flush()
            fallback_price = int(
                seed.get("fallback_price") or settings.sumistore_fallback_price
            )
            session.add(
                Product(
                    category_id=category.id,
                    name_vi=str(seed.get("name_vi") or supplier_product_id),
                    name_en=str(seed.get("name_en") or supplier_product_id),
                    description_vi=(
                        "Tài khoản giao tự động từ Sumistore. Đọc kỹ mô tả và chính sách "
                        "bảo hành trước khi mua."
                    ),
                    description_en=(
                        "Automatically delivered from Sumistore. Read the description and "
                        "warranty policy before purchase."
                    ),
                    price=fallback_price + settings.sumistore_markup,
                    product_type="account",
                    allow_quantity=True,
                    max_quantity=10,
                    fulfillment_source="sumistore",
                    supplier_product_id=supplier_product_id,
                    supplier_markup=settings.sumistore_markup,
                    supplier_price=fallback_price,
                    external_stock=0,
                )
            )
        await session.commit()


async def refresh_external_product(
    session: AsyncSession,
    product: Product,
    client: SumistoreClient | None,
) -> int:
    if product.fulfillment_source != "sumistore" or not product.supplier_product_id:
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
            "Supplier sync failed for product %s: code=%s",
            product.supplier_product_id,
            exc.code,
        )
        return product.external_stock
    product.external_stock = snapshot.effective_stock + recovered_stock
    await apply_supplier_price(session, product, snapshot.unit_price)
    product.supplier_synced_at = datetime.now(UTC)
    await session.flush()
    return product.external_stock


async def sync_sumistore_products(
    session_factory: async_sessionmaker[AsyncSession],
    client: SumistoreClient | None,
) -> None:
    async with session_factory() as session:
        products = list(
            await session.scalars(
                select(Product).where(
                    Product.fulfillment_source == "sumistore",
                    Product.active.is_(True),
                )
            )
        )
        for product in products:
            await refresh_external_product(session, product, client)
        await session.commit()
