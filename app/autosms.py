import asyncio
import re
import time

import httpx

from app.config import Settings
from app.rentsim import RentSimError, RentSimOtp, RentSimRental, RentSimSnapshot


class AutoSmsClient:
    provider = "autosms"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        country_id: str = "us",
        service_id: str = "chatgpt",
        unit_price: int = 1_000,
        timeout_seconds: float = 15,
        snapshot_cache_seconds: int = 10,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.country_id = country_id
        self.service_id = service_id
        self.unit_price = unit_price
        self.timeout_seconds = timeout_seconds
        self.snapshot_cache_seconds = snapshot_cache_seconds
        self.transport = transport
        self.balance_lock = asyncio.Lock()
        self._snapshot_lock = asyncio.Lock()
        self._snapshot: RentSimSnapshot | None = None
        self._snapshot_at = 0.0
        self._out_of_stock_until = 0.0
        self._http_client: httpx.AsyncClient | None = None

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

    @staticmethod
    def _error(payload: dict[str, object], fallback: str) -> RentSimError:
        message = str(payload.get("message") or payload.get("error") or fallback).strip()
        normalized = message.casefold()
        if "key" in normalized or "không hợp lệ" in normalized:
            return RentSimError("INVALID_KEY", message)
        if any(marker in normalized for marker in ("hết số", "hết hàng", "out of stock")):
            return RentSimError("OUT_OF_STOCK", message)
        if any(marker in normalized for marker in ("số dư", "balance")):
            return RentSimError("INSUFFICIENT_BALANCE", message)
        if any(marker in normalized for marker in ("không tồn tại", "not found")):
            return RentSimError("ORDER_NOT_FOUND", message)
        if any(marker in normalized for marker in ("quá nhiều", "rate limit")):
            return RentSimError("RATE_LIMITED", message)
        return RentSimError(fallback, message)

    async def _get(self, path: str) -> dict[str, object]:
        try:
            response = await self._http().get(
                f"{self.base_url}/{path.lstrip('/')}",
                params={"key": self.api_key},
            )
        except httpx.HTTPError as exc:
            raise RentSimError("PROVIDER_UNAVAILABLE", type(exc).__name__) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise RentSimError("INVALID_RESPONSE") from exc
        if not isinstance(payload, dict):
            raise RentSimError("INVALID_RESPONSE")
        if response.status_code in {401, 403}:
            raise self._error(payload, "INVALID_KEY")
        if response.status_code == 429:
            raise self._error(payload, "RATE_LIMITED")
        if response.is_error:
            raise self._error(payload, f"PROVIDER_HTTP_{response.status_code}")
        if payload.get("success") is False:
            raise self._error(payload, "PROVIDER_ERROR")
        return payload

    @staticmethod
    def _data(payload: dict[str, object]) -> dict[str, object]:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RentSimError("INVALID_RESPONSE")
        return data

    async def fetch_balance(self) -> int:
        data = self._data(await self._get("api/balance"))
        if str(data.get("status") or "active").casefold() != "active":
            raise RentSimError("ACCOUNT_INACTIVE")
        try:
            return max(0, int(float(str(data.get("balance") or 0))))
        except (TypeError, ValueError) as exc:
            raise RentSimError("INVALID_RESPONSE") from exc

    async def fetch_snapshot(self, *, force: bool = False) -> RentSimSnapshot:
        now = time.monotonic()
        if (
            not force
            and self._snapshot is not None
            and now - self._snapshot_at < self.snapshot_cache_seconds
        ):
            return self._snapshot
        async with self._snapshot_lock:
            now = time.monotonic()
            if (
                not force
                and self._snapshot is not None
                and now - self._snapshot_at < self.snapshot_cache_seconds
            ):
                return self._snapshot
            balance = await self.fetch_balance()
            estimated_stock = balance // self.unit_price if self.unit_price > 0 else 0
            snapshot = RentSimSnapshot(
                service_id=self.service_id,
                service_name="ChatGPT",
                server_id=self.country_id,
                unit_price=self.unit_price,
                source_stock=estimated_stock,
                balance=balance,
                availability_blocked=now < self._out_of_stock_until,
            )
            self._snapshot = snapshot
            self._snapshot_at = now
            return snapshot

    def invalidate_snapshot(self) -> None:
        self._snapshot_at = 0.0

    @staticmethod
    def rent_error_is_ambiguous(code: str) -> bool:
        if code in {"PROVIDER_UNAVAILABLE", "INVALID_RESPONSE"}:
            return True
        if not code.startswith("PROVIDER_HTTP_"):
            return False
        try:
            return int(code.rsplit("_", 1)[1]) >= 500
        except ValueError:
            return False

    @staticmethod
    def _otp_code(data: dict[str, object]) -> str:
        code = str(data.get("code") or "").strip()
        if code:
            return code
        content = str(data.get("message") or "").strip()
        match = re.search(r"(?<!\d)(\d{4,8})(?!\d)", content)
        return match.group(1) if match else ""

    async def rent(self) -> RentSimRental:
        try:
            payload = await self._get(
                f"api/buy-number/{self.country_id}/{self.service_id}"
            )
        except RentSimError as exc:
            if exc.code == "OUT_OF_STOCK":
                self._out_of_stock_until = time.monotonic() + 60
                self.invalidate_snapshot()
            raise
        data = self._data(payload)
        order_id = str(data.get("order_id") or data.get("id") or "").strip()
        phone_number = str(data.get("phone") or data.get("phone_number") or "").strip()
        try:
            unit_price = int(float(str(data.get("price") or 0)))
        except (TypeError, ValueError) as exc:
            raise RentSimError("INVALID_RESPONSE") from exc
        if not order_id or not phone_number or unit_price <= 0:
            raise RentSimError("INVALID_RESPONSE")
        self.unit_price = unit_price
        self._out_of_stock_until = 0.0
        self.invalidate_snapshot()
        return RentSimRental(
            order_id=order_id,
            status="pending",
            phone_number=phone_number,
            phone_number_display=phone_number,
            country_code="+1",
            service_name="ChatGPT",
            unit_price=unit_price,
        )

    async def cancel(self, order_id: str) -> bool:
        payload = await self._get(f"api/cancel/{order_id}")
        if payload.get("success") is not True:
            raise self._error(payload, "CANCEL_FAILED")
        self.invalidate_snapshot()
        return True

    async def fetch_otp(self, order_id: str) -> RentSimOtp:
        data = self._data(await self._get(f"api/orders/{order_id}"))
        code = self._otp_code(data)
        content = str(data.get("message") or "").strip()
        status = str(data.get("status") or "pending").casefold()
        if code:
            return RentSimOtp(
                status="success",
                order_id=str(data.get("id") or order_id),
                service_name=str(data.get("service") or "ChatGPT"),
                code=code,
                content=content,
            )
        if status in {"cancelled", "canceled", "failed", "refunded"}:
            self.invalidate_snapshot()
            return RentSimOtp(status="failed", order_id=order_id)
        remaining_value = data.get("remaining_seconds")
        try:
            remaining_seconds = (
                int(float(str(remaining_value))) if remaining_value is not None else -1
            )
        except (TypeError, ValueError):
            remaining_seconds = -1
        if status == "expired" or remaining_seconds == 0:
            await self.cancel(order_id)
            return RentSimOtp(status="timeout", order_id=order_id)
        return RentSimOtp(
            status="pending",
            order_id=str(data.get("id") or order_id),
            service_name=str(data.get("service") or "ChatGPT"),
        )


def create_autosms_client(settings: Settings) -> AutoSmsClient | None:
    api_key = settings.autosms_api_key.get_secret_value()
    if not settings.autosms_enabled or not api_key:
        return None
    return AutoSmsClient(
        settings.autosms_base_url,
        api_key,
        country_id=settings.autosms_country_id,
        service_id=settings.autosms_service_id,
        unit_price=settings.autosms_fallback_price,
        timeout_seconds=settings.autosms_timeout_seconds,
        snapshot_cache_seconds=settings.autosms_snapshot_cache_seconds,
    )
