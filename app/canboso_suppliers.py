import asyncio
import math
import secrets
import time
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import httpx

from app.config import Settings
from app.suppliers import SupplierError, SupplierPurchase, SupplierSnapshot


CANBOSO_GG18M_ROUTE_ID = "gg18m"


@dataclass(frozen=True)
class CanbosoProduct:
    product_id: str
    name: str
    description: str
    unit_price: int
    stock: int


def _plain_text(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return " ".join(
        "".join(character for character in normalized if not unicodedata.combining(character))
        .lower()
        .replace("_", " ")
        .replace("-", " ")
        .split()
    )


def _vnd_amount(value: object, currency: str, usd_to_vnd: int) -> int:
    try:
        amount = Decimal(str(value or 0))
    except (InvalidOperation, ValueError) as exc:
        raise SupplierError("SUPPLIER_INVALID_RESPONSE") from exc
    if amount <= 0:
        return 0
    if currency.upper() == "USD":
        amount *= Decimal(usd_to_vnd)
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class CanbosoClient:
    provider = "canboso"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        usd_to_vnd: int = 27_500,
        configured_product_id: str = "",
        timeout_seconds: float = 15,
        snapshot_cache_seconds: int = 10,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.usd_to_vnd = usd_to_vnd
        self.configured_product_id = configured_product_id.strip()
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
        self._purchase_blocked_until = 0.0

    def _http(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
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

    def purchase_is_blocked(self, _product_id: str) -> bool:
        return self._purchase_blocked_until > time.monotonic()

    def _block_from_response(self, response: httpx.Response) -> None:
        try:
            retry_after = max(1, int(response.headers.get("Retry-After") or 60))
        except ValueError:
            retry_after = 60
        self._purchase_blocked_until = time.monotonic() + retry_after

    @staticmethod
    def _payload_error(payload: object, fallback: str) -> SupplierError:
        if not isinstance(payload, dict):
            return SupplierError(fallback)
        message = str(
            payload.get("message")
            or payload.get("error")
            or payload.get("detail")
            or fallback
        )
        normalized = _plain_text(message)
        code = str(payload.get("code") or fallback)
        if "stock" in normalized or "inventory" in normalized or "het hang" in normalized:
            code = "INSUFFICIENT_STOCK"
        elif "balance" in normalized or "wallet" in normalized or "so du" in normalized:
            code = "INSUFFICIENT_BALANCE"
        elif "idempotency" in normalized and "progress" in normalized:
            code = "SUPPLIER_REQUEST_IN_PROGRESS"
        return SupplierError(code, message)

    async def _decode_response(self, response: httpx.Response) -> dict[str, object]:
        if response.status_code == 429:
            self._block_from_response(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SupplierError("SUPPLIER_INVALID_RESPONSE") from exc
        if response.is_error or not isinstance(payload, dict) or payload.get("success") is False:
            if response.status_code == 429:
                message = (
                    str(payload.get("message") or payload.get("error") or "rate_limited")
                    if isinstance(payload, dict)
                    else "rate_limited"
                )
                raise SupplierError("SUPPLIER_PURCHASE_BACKOFF", message)
            fallback = (
                f"SUPPLIER_HTTP_{response.status_code}"
            )
            raise self._payload_error(payload, fallback)
        return payload

    async def _get(self, path: str) -> dict[str, object]:
        try:
            response = await self._http().get(
                f"{self.base_url}/{path.lstrip('/')}",
                params={"key": self.api_key},
            )
        except httpx.HTTPError as exc:
            raise SupplierError("SUPPLIER_UNAVAILABLE", type(exc).__name__) from exc
        return await self._decode_response(response)

    async def _post(
        self,
        path: str,
        body: dict[str, object],
        *,
        idempotency_key: str,
    ) -> dict[str, object]:
        try:
            response = await self._http().post(
                f"{self.base_url}/{path.lstrip('/')}",
                json=body,
                headers={"Idempotency-Key": idempotency_key},
            )
        except httpx.HTTPError as exc:
            raise SupplierError("SUPPLIER_UNAVAILABLE", type(exc).__name__) from exc
        return await self._decode_response(response)

    def _matches_gg18m(self, raw_product: dict[str, object]) -> bool:
        product_id = str(
            raw_product.get("productId")
            or raw_product.get("_id")
            or raw_product.get("id")
            or ""
        ).strip()
        if self.configured_product_id:
            return product_id == self.configured_product_id
        name = _plain_text(
            f"{raw_product.get('name') or ''} "
            f"{raw_product.get('product_name') or ''} "
            f"{raw_product.get('product_name_raw') or ''} "
            f"{raw_product.get('description_raw') or ''} "
            f"{raw_product.get('description') or ''}"
        )
        has_duration = "18m" in name or "18 month" in name or "18 thang" in name
        has_product = any(token in name for token in ("jio", "gg pro", "google pro", "gemini"))
        return has_duration and has_product

    async def fetch_products(self) -> tuple[CanbosoProduct, ...]:
        payload = await self._get("api/v2/telegram-buyer/products")
        products = payload.get("products")
        if not isinstance(products, list):
            raise SupplierError("SUPPLIER_INVALID_RESPONSE")
        payload_currency = str(payload.get("walletCurrency") or "USD").upper()
        values: list[CanbosoProduct] = []
        for raw_product in products:
            if not isinstance(raw_product, dict) or not self._matches_gg18m(raw_product):
                continue
            stats = raw_product.get("stats")
            stats_data = stats if isinstance(stats, dict) else {}
            availability = raw_product.get("availability")
            availability_data = availability if isinstance(availability, dict) else {}
            price = raw_product.get("price")
            price_data = price if isinstance(price, dict) else {}
            currency = str(
                price_data.get("currency")
                or raw_product.get("walletCurrency")
                or payload_currency
            ).upper()
            price_value = raw_product.get("walletPricing")
            if price_value is None and currency == "USD":
                price_value = raw_product.get("usdPricing")
            if price_value is None:
                price_value = price_data.get("amount")
            try:
                unit_price = _vnd_amount(price_value, currency, self.usd_to_vnd)
                stock = int(
                    availability_data.get("available")
                    if availability_data.get("available") is not None
                    else stats_data.get("available") or 0
                )
            except (TypeError, ValueError):
                continue
            product_id = str(
                raw_product.get("productId")
                or raw_product.get("_id")
                or raw_product.get("id")
                or ""
            ).strip()
            if product_id and unit_price > 0:
                values.append(
                    CanbosoProduct(
                        product_id=product_id,
                        name=str(
                            raw_product.get("name")
                            or raw_product.get("product_name")
                            or product_id
                        ),
                        description=str(raw_product.get("description") or ""),
                        unit_price=unit_price,
                        stock=max(0, stock),
                    )
                )
        return tuple(values)

    async def fetch_balance(self) -> int:
        payload = await self._get("api/v2/telegram-buyer/balance")
        currency = str(payload.get("walletCurrency") or "USD").upper()
        if currency == "USD":
            raw_balance = payload.get("balanceUsd")
            if raw_balance is None:
                raw_balance = payload.get("balance")
        else:
            raw_balance = payload.get("balanceVnd")
            if raw_balance is None:
                raw_balance = payload.get("balance")
        return _vnd_amount(raw_balance, currency, self.usd_to_vnd)

    async def fetch_snapshot(self, product_id: str) -> SupplierSnapshot:
        if self.purchase_is_blocked(product_id):
            raise SupplierError("SUPPLIER_PURCHASE_BACKOFF")
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
                if not products:
                    raise SupplierError("SUPPLIER_PRODUCT_MISSING")
                product = min(
                    products,
                    key=lambda item: (
                        item.stock <= 0,
                        item.unit_price,
                        item.product_id,
                    ),
                )
                self._resolved_product_ids[CANBOSO_GG18M_ROUTE_ID] = product.product_id
                self._snapshots = {
                    CANBOSO_GG18M_ROUTE_ID: SupplierSnapshot(
                        product_id=CANBOSO_GG18M_ROUTE_ID,
                        name=product.name,
                        description=product.description,
                        unit_price=product.unit_price,
                        source_stock=product.stock,
                        owner_balance=max(0, balance),
                    )
                }
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
        if self.purchase_is_blocked(product_id):
            raise SupplierError("SUPPLIER_PURCHASE_BACKOFF")
        if product_id not in self._resolved_product_ids:
            await self.fetch_snapshot(product_id)
        resolved_product_id = self._resolved_product_ids.get(product_id)
        if not resolved_product_id:
            raise SupplierError("SUPPLIER_PRODUCT_MISSING")
        request_key = idempotency_key or f"shop-{secrets.token_hex(12)}"
        try:
            payload = await self._post(
                "api/v2/telegram-buyer/purchase",
                {
                    "key": self.api_key,
                    "product_id": resolved_product_id,
                    "quantity": quantity,
                },
                idempotency_key=request_key,
            )
        except SupplierError as exc:
            if exc.code == "INSUFFICIENT_STOCK":
                self.invalidate_snapshot_cache()
                self._purchase_blocked_until = max(
                    self._purchase_blocked_until,
                    time.monotonic() + 5,
                )
            raise
        self.invalidate_snapshot_cache()
        delivery = payload.get("delivery")
        delivery_data = delivery if isinstance(delivery, dict) else {}
        delivered = (
            payload.get("deliveredAccounts")
            or payload.get("delivered_accounts")
            or delivery_data.get("accounts")
        )
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
        payment = payload.get("payment")
        payment_data = payment if isinstance(payment, dict) else {}
        currency = str(
            payment_data.get("currency") or payload.get("walletCurrency") or "USD"
        ).upper()
        total_value = payload.get("amountUsd") if currency == "USD" else payload.get("amount")
        if total_value is None:
            total_value = payment_data.get("amount")
        total_amount = _vnd_amount(total_value, currency, self.usd_to_vnd)
        if total_amount <= 0:
            raise SupplierError("SUPPLIER_INVALID_RESPONSE")
        order = payload.get("order")
        order_data = order if isinstance(order, dict) else {}
        raw_order_code = str(
            payload.get("orderCode") or order_data.get("orderCode") or request_key
        ).strip()
        return SupplierPurchase(
            order_code=f"CBS-{raw_order_code}",
            unit_price=math.ceil(total_amount / quantity),
            accounts=tuple(accounts),
            product_id=product_id,
            provider=self.provider,
        )


def create_canboso_client(settings: Settings) -> CanbosoClient | None:
    api_key = settings.canboso_api_key.get_secret_value()
    if not settings.canboso_enabled or not api_key:
        return None
    return CanbosoClient(
        settings.canboso_base_url,
        api_key,
        usd_to_vnd=settings.canboso_usd_to_vnd,
        configured_product_id=settings.canboso_gg18m_product_id,
        timeout_seconds=settings.canboso_timeout_seconds,
        snapshot_cache_seconds=settings.supplier_ui_cache_seconds,
    )
