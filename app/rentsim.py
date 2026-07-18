import asyncio
import time
from dataclasses import dataclass

import httpx

from app.config import Settings


class RentSimError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


@dataclass(frozen=True)
class RentSimSnapshot:
    service_id: str
    service_name: str
    server_id: str
    unit_price: int
    source_stock: int
    balance: int

    @property
    def effective_stock(self) -> int:
        if self.unit_price <= 0:
            return 0
        # RentSim's catalog stock is stale (kh2 can rent while reporting zero),
        # so wallet capacity is the only reliable pre-purchase estimate.
        return max(0, self.balance // self.unit_price)


@dataclass(frozen=True)
class RentSimRental:
    order_id: str
    status: str
    phone_number: str
    phone_number_display: str
    country_code: str
    service_name: str
    otp_code: str = ""
    otp_content: str = ""


@dataclass(frozen=True)
class RentSimOtp:
    status: str
    order_id: str
    service_name: str = ""
    code: str = ""
    content: str = ""


class RentSimClient:
    provider = "rentsim"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        server_id: str = "kh2",
        service_id: str = "chatgpt",
        timeout_seconds: float = 15,
        snapshot_cache_seconds: int = 10,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.server_id = server_id
        self.service_id = service_id
        self.timeout_seconds = timeout_seconds
        self.snapshot_cache_seconds = snapshot_cache_seconds
        self.transport = transport
        self.balance_lock = asyncio.Lock()
        self._snapshot_lock = asyncio.Lock()
        self._snapshot: RentSimSnapshot | None = None
        self._snapshot_at = 0.0

    async def _get(self, path: str, *, params: dict[str, str] | None = None) -> dict[str, object]:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.get(
                    f"{self.base_url}/{path.lstrip('/')}",
                    params=params,
                )
        except httpx.HTTPError as exc:
            raise RentSimError("PROVIDER_UNAVAILABLE", type(exc).__name__) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise RentSimError("INVALID_RESPONSE") from exc
        if response.is_error or not isinstance(payload, dict):
            raise RentSimError(f"PROVIDER_HTTP_{response.status_code}")
        return payload

    @staticmethod
    def _error(payload: dict[str, object], fallback: str) -> RentSimError:
        message = str(payload.get("message") or payload.get("error") or fallback).strip()
        normalized = message.lower()
        if "token" in normalized or "key" in normalized:
            return RentSimError("INVALID_KEY", message)
        if "stock" in normalized or "hết hàng" in normalized:
            return RentSimError("OUT_OF_STOCK", message)
        if "timeout" in normalized:
            return RentSimError("TIMEOUT", message)
        return RentSimError(fallback, message)

    async def fetch_balance(self) -> int:
        try:
            payload = await self._get(f"getbalance/{self.api_key}")
        except RentSimError as exc:
            if exc.code != "PROVIDER_HTTP_404":
                raise
            payload = await self._get(self.api_key)
        if str(payload.get("status") or "").lower() == "error":
            raise self._error(payload, "BALANCE_ERROR")
        try:
            return max(0, int(float(str(payload.get("balance") or 0))))
        except (TypeError, ValueError) as exc:
            raise RentSimError("INVALID_RESPONSE") from exc

    async def fetch_services(self) -> dict[str, list[dict[str, object]]]:
        payload = await self._get("phone/services")
        if str(payload.get("status") or "").lower() == "error":
            raise self._error(payload, "SERVICES_ERROR")
        services: dict[str, list[dict[str, object]]] = {}
        for server_id, raw_services in payload.items():
            if isinstance(raw_services, list):
                services[str(server_id)] = [
                    item for item in raw_services if isinstance(item, dict)
                ]
        return services

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
            services, balance = await asyncio.gather(
                self.fetch_services(),
                self.fetch_balance(),
            )
            server_services = next(
                (
                    values
                    for key, values in services.items()
                    if key.lower() == self.server_id.lower()
                ),
                None,
            )
            if server_services is None:
                raise RentSimError("SERVER_NOT_FOUND")
            service = next(
                (
                    item
                    for item in server_services
                    if str(item.get("value") or "").lower() == self.service_id.lower()
                ),
                None,
            )
            if service is None:
                raise RentSimError("SERVICE_NOT_FOUND")
            try:
                unit_price = int(float(str(service.get("price") or 0)))
                source_stock = int(float(str(service.get("stock") or 0)))
            except (TypeError, ValueError) as exc:
                raise RentSimError("INVALID_RESPONSE") from exc
            if unit_price <= 0:
                raise RentSimError("INVALID_RESPONSE")
            snapshot = RentSimSnapshot(
                service_id=str(service.get("value") or self.service_id),
                service_name=str(service.get("name") or self.service_id),
                server_id=self.server_id,
                unit_price=unit_price,
                source_stock=max(0, source_stock),
                balance=balance,
            )
            self._snapshot = snapshot
            self._snapshot_at = now
            return snapshot

    def invalidate_snapshot(self) -> None:
        self._snapshot_at = 0.0

    async def rent(self) -> RentSimRental:
        payload = await self._get(
            f"api/{self.service_id}/{self.api_key}",
            params={"server": self.server_id},
        )
        if str(payload.get("status") or "").lower() == "error":
            raise self._error(payload, "RENT_ERROR")
        order_id = str(payload.get("id") or "").strip()
        phone_number = str(payload.get("phoneNumber") or "").strip()
        if not order_id or not phone_number:
            raise RentSimError("INVALID_RESPONSE")
        status_text = str(payload.get("status") or "Pending").lower()
        status = "success" if status_text in {"success", "successed", "completed"} else "pending"
        self.invalidate_snapshot()
        return RentSimRental(
            order_id=order_id,
            status=status,
            phone_number=phone_number,
            phone_number_display=str(payload.get("phonenoprefix") or phone_number),
            country_code=str(payload.get("coutrycode") or payload.get("countrycode") or ""),
            service_name=str(payload.get("serviceName") or self.service_id),
            otp_code=str(payload.get("code") or ""),
            otp_content=str(payload.get("content") or ""),
        )

    async def fetch_otp(self, order_id: str) -> RentSimOtp:
        payload = await self._get(f"api/order/{order_id}/{self.api_key}")
        status_text = str(payload.get("status") or "").lower()
        message = str(payload.get("message") or "")
        if status_text == "error":
            error = self._error(payload, "OTP_ERROR")
            if error.code == "TIMEOUT":
                self.invalidate_snapshot()
                return RentSimOtp(status="timeout", order_id=order_id)
            raise error
        if status_text in {"success", "successed", "completed"}:
            return RentSimOtp(
                status="success",
                order_id=str(payload.get("id") or order_id),
                service_name=str(payload.get("serviceName") or self.service_id),
                code=str(payload.get("code") or ""),
                content=str(payload.get("content") or ""),
            )
        # RentSim reports refunded/failed orders with a terminal status rather
        # than the documented Timeout error payload. Do not leave these orders
        # pending or the user's wallet will remain charged indefinitely.
        if status_text in {"failed", "cancelled", "canceled", "expired", "rejected"}:
            self.invalidate_snapshot()
            return RentSimOtp(
                status="failed",
                order_id=str(payload.get("id") or order_id),
                service_name=str(payload.get("serviceName") or self.service_id),
            )
        if "timeout" in message.lower():
            self.invalidate_snapshot()
            return RentSimOtp(status="timeout", order_id=order_id)
        return RentSimOtp(
            status="pending",
            order_id=str(payload.get("id") or order_id),
            service_name=str(payload.get("serviceName") or self.service_id),
        )


def create_rentsim_client(settings: Settings) -> RentSimClient | None:
    api_key = settings.rentsim_api_key.get_secret_value()
    if not settings.rentsim_enabled or not api_key:
        return None
    return RentSimClient(
        settings.rentsim_base_url,
        api_key,
        server_id=settings.rentsim_server_id,
        service_id=settings.rentsim_service_id,
        timeout_seconds=settings.rentsim_timeout_seconds,
        snapshot_cache_seconds=settings.rentsim_snapshot_cache_seconds,
    )
