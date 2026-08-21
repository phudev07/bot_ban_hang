"""Authenticated API for importing encrypted inventory from automation tools."""

import hashlib
import hmac
import ipaddress
import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.inventory_import import InventoryImportError, import_inventory
from app.models import InventoryItem, Product, WarehouseImportRequest
from app.partner_services import api_signature
from app.public_api import client_ip
from app.rate_limit import FixedWindowRateLimiter, RateLimitDecision, RateLimitRule
from app.suppliers import EXTERNAL_FULFILLMENT_SOURCES, SELLABLE_FULFILLMENT_SOURCES
from app.utils import SecretCipher


logger = logging.getLogger(__name__)
WAREHOUSE_SIGNATURE_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
WAREHOUSE_NONCE_PATTERN = re.compile(r"^[A-Za-z0-9._~:-]{12,128}$")
WAREHOUSE_IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
WAREHOUSE_STALE_REQUEST_AFTER = timedelta(minutes=15)


class WarehouseImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: int = Field(ge=1)
    items: list[str] = Field(default_factory=list, max_length=5_000)
    items_text: str | None = Field(default=None, max_length=2_000_000)
    cost_amount: int | str
    import_note_id: int | None = Field(default=None, ge=1)
    new_import_note: str = Field(default="", max_length=255)
    lock_sale_price: bool = False
    notify_stock_arrival: bool = False

    @model_validator(mode="before")
    @classmethod
    def normalize_items_input(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        supplied_items = data.get("items")
        items_text = data.get("items_text")
        if isinstance(supplied_items, str):
            items_text = supplied_items if not items_text else items_text
            data["items"] = []
        if not data.get("items") and isinstance(items_text, str):
            normalized = items_text.replace("\r\n", "\n").strip()
            if "\n---\n" in normalized:
                data["items"] = [item.strip() for item in normalized.split("\n---\n") if item.strip()]
            else:
                data["items"] = [line.strip() for line in normalized.splitlines() if line.strip()]
        return data

    @model_validator(mode="after")
    def validate_item_size(self) -> "WarehouseImportBody":
        if any(len(item) > 8_192 for item in self.items):
            raise ValueError("Each inventory item is too large")
        return self


def warehouse_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _allowed_ips(value: str) -> set[str]:
    addresses: set[str] = set()
    for raw in value.replace("\n", ",").split(","):
        candidate = raw.strip()
        if not candidate:
            continue
        try:
            addresses.add(str(ipaddress.ip_address(candidate)))
        except ValueError:
            logger.warning("Ignoring invalid WAREHOUSE_API_ALLOWED_IPS entry")
    return addresses


def _request_path(request: Request) -> str:
    return request.url.path + (f"?{request.url.query}" if request.url.query else "")


def _rate_limited_error(decision: RateLimitDecision) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={"code": "RATE_LIMITED", "message": "Too many requests"},
        headers={"Retry-After": str(decision.retry_after), "Cache-Control": "no-store"},
    )


def _result_payload(request_id: int, result: object) -> dict[str, object]:
    return {
        "success": True,
        "request_id": request_id,
        "status": "completed",
        "product_id": result.product_id,
        "product": result.product_name,
        "accepted_count": result.accepted_count,
        "duplicate_count": result.duplicate_count,
        "cost_amount": result.cost_amount,
        "import_note": result.import_note,
        "stock_before": result.stock_before,
        "stock_after": result.stock_after,
        "price_locked": result.lock_applied,
        "notification_queued": result.notification_queued,
    }


def create_warehouse_api_router(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    cipher: SecretCipher,
    redis_client: Redis,
) -> APIRouter:
    router = APIRouter(prefix="/v1/warehouse", tags=["warehouse-import"])
    limiter = FixedWindowRateLimiter(redis_client, "warehouse-import-limit")

    async def authenticate(
        request: Request,
        x_timestamp: str = Header(default="", alias="X-Timestamp"),
        x_nonce: str = Header(default="", alias="X-Nonce"),
        x_signature: str = Header(default="", alias="X-Signature"),
    ) -> None:
        if not settings.warehouse_api_enabled:
            raise warehouse_error(503, "API_DISABLED", "Warehouse import API is disabled")
        secret = settings.warehouse_api_key.get_secret_value()
        if not secret:
            raise warehouse_error(503, "API_NOT_CONFIGURED", "Warehouse import API is not configured")
        remote_ip = client_ip(request) or "unknown"
        allowed = _allowed_ips(settings.warehouse_api_allowed_ips_text)
        if allowed and remote_ip not in allowed:
            raise warehouse_error(403, "IP_NOT_ALLOWED", "Client IP is not allowed")
        if not all((x_timestamp, x_nonce, x_signature)):
            raise warehouse_error(401, "AUTH_REQUIRED", "Missing warehouse API authentication headers")
        if not WAREHOUSE_SIGNATURE_PATTERN.fullmatch(x_signature):
            raise warehouse_error(401, "INVALID_SIGNATURE", "Request signature is invalid")
        if not WAREHOUSE_NONCE_PATTERN.fullmatch(x_nonce):
            raise warehouse_error(401, "INVALID_NONCE", "Nonce must contain 12-128 safe ASCII characters")
        try:
            timestamp = int(x_timestamp)
        except ValueError as exc:
            raise warehouse_error(401, "INVALID_TIMESTAMP", "Invalid timestamp") from exc
        now = int(time.time())
        if abs(now - timestamp) > settings.warehouse_api_signature_tolerance_seconds:
            raise warehouse_error(401, "EXPIRED_REQUEST", "Request timestamp is outside the allowed window")
        body = await request.body()
        expected = api_signature(
            secret,
            x_timestamp,
            x_nonce,
            request.method,
            _request_path(request),
            body,
        )
        if not hmac.compare_digest(expected, x_signature.lower()):
            raise warehouse_error(401, "INVALID_SIGNATURE", "Request signature is invalid")
        try:
            nonce_added = await redis_client.set(
                f"warehouse-import:nonce:{x_nonce}",
                "1",
                ex=settings.warehouse_api_signature_tolerance_seconds,
                nx=True,
            )
            if not nonce_added:
                raise warehouse_error(409, "REPLAYED_REQUEST", "Nonce has already been used")
            per_ip = await limiter.hit(
                f"ip:{remote_ip}",
                (RateLimitRule("burst", 20, 10), RateLimitRule("minute", settings.warehouse_api_rate_limit_per_minute, 60)),
            )
            if not per_ip.allowed:
                raise _rate_limited_error(per_ip)
            global_limit = await limiter.hit(
                "global",
                (RateLimitRule("minute", settings.warehouse_api_global_rate_limit_per_minute, 60),),
            )
            if not global_limit.allowed:
                raise _rate_limited_error(global_limit)
        except HTTPException:
            raise
        except RedisError as exc:
            raise warehouse_error(503, "AUTH_STORE_UNAVAILABLE", "Warehouse API authentication is unavailable") from exc

    @router.get("/health")
    async def warehouse_health() -> dict[str, object]:
        return {"success": True, "enabled": settings.warehouse_api_enabled}

    @router.get("/inventory/stock")
    async def inventory_stock_endpoint(
        product_id: int = Query(ge=1),
        _auth: None = Depends(authenticate),
    ) -> dict[str, object]:
        """Return safe stock counts for the automation tool before importing."""
        async with session_factory() as session:
            product = await session.scalar(select(Product).where(Product.id == product_id))
            if (
                product is None
                or product.archived_at is not None
                or product.product_type != "account"
                or product.fulfillment_source not in SELLABLE_FULFILLMENT_SOURCES
            ):
                raise warehouse_error(404, "PRODUCT_NOT_FOUND", "Product is not available")
            if not product.active:
                raise warehouse_error(409, "PRODUCT_HIDDEN", "Product is hidden")
            local_stock = int(
                await session.scalar(
                    select(func.count(InventoryItem.id)).where(
                        InventoryItem.product_id == product.id,
                        InventoryItem.status == "available",
                    )
                )
                or 0
            )
            source_stock = (
                max(0, int(product.supplier_available_stock))
                if product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES
                else 0
            )
            total_stock = local_stock + source_stock
            return {
                "success": True,
                "product_id": product.id,
                "product": product.name_vi,
                "local_stock": local_stock,
                "source_stock": source_stock,
                "total_stock": total_stock,
                "has_stock": total_stock > 0,
                "can_import": True,
            }

    @router.post("/inventory/import")
    async def import_inventory_endpoint(
        request: Request,
        body: WarehouseImportBody,
        _auth: None = Depends(authenticate),
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    ) -> dict[str, object]:
        normalized_key = idempotency_key.strip()
        if not WAREHOUSE_IDEMPOTENCY_PATTERN.fullmatch(normalized_key):
            raise warehouse_error(400, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key must contain 8-128 safe ASCII characters")
        if len(body.items) > settings.warehouse_api_max_items_per_request:
            raise warehouse_error(413, "TOO_MANY_ITEMS", "Too many inventory items in one request")
        request_json = json.dumps(body.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        request_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        async with session_factory() as session:
            product_exists = await session.scalar(
                select(Product.id).where(Product.id == body.product_id)
            )
            if product_exists is None:
                raise warehouse_error(404, "PRODUCT_NOT_FOUND", "Product does not exist")
            existing = await session.scalar(
                select(WarehouseImportRequest)
                .where(WarehouseImportRequest.idempotency_key == normalized_key)
                .with_for_update()
            )
            claimed = False
            if existing is None:
                existing = WarehouseImportRequest(
                    idempotency_key=normalized_key,
                    request_hash=request_hash,
                    product_id=body.product_id,
                )
                session.add(existing)
                try:
                    await session.flush()
                    claimed = True
                except IntegrityError:
                    await session.rollback()
                    existing = await session.scalar(
                        select(WarehouseImportRequest)
                        .where(WarehouseImportRequest.idempotency_key == normalized_key)
                        .with_for_update()
                    )
            if existing is None:
                raise warehouse_error(409, "IDEMPOTENCY_CONFLICT", "Could not claim import request")
            if existing.request_hash != request_hash:
                raise warehouse_error(409, "IDEMPOTENCY_MISMATCH", "Idempotency-Key was already used with another payload")
            if not claimed:
                if existing.status == "completed" and existing.response_json:
                    return json.loads(existing.response_json)
                if existing.status == "processing":
                    updated_at = existing.updated_at or existing.created_at
                    if updated_at is not None and updated_at.tzinfo is None:
                        updated_at = updated_at.replace(tzinfo=UTC)
                    if updated_at and updated_at > datetime.now(UTC) - WAREHOUSE_STALE_REQUEST_AFTER:
                        raise warehouse_error(409, "REQUEST_IN_PROGRESS", "The original import is still processing")
                    existing.status = "processing"
                    existing.error_code = None
                elif existing.status == "failed":
                    raise warehouse_error(409, existing.error_code or "PREVIOUS_REQUEST_FAILED", "The original import failed")
                else:
                    raise warehouse_error(409, "REQUEST_IN_PROGRESS", "The original import is still processing")
            try:
                result = await import_inventory(
                    session,
                    cipher,
                    product_id=body.product_id,
                    raw_items=body.items,
                    cost_amount=body.cost_amount,
                    import_note_id=body.import_note_id,
                    new_import_note=body.new_import_note,
                    lock_sale_price=body.lock_sale_price,
                    notify_stock_arrival=body.notify_stock_arrival,
                )
            except InventoryImportError as exc:
                existing.status = "failed"
                existing.error_code = str(exc)
                await session.commit()
                error_map = {
                    "COST_INVALID": (400, "COST_INVALID", "cost_amount is invalid"),
                    "ITEMS_EMPTY": (400, "ITEMS_EMPTY", "No inventory items were supplied"),
                    "IMPORT_NOTE_TOO_LONG": (400, "IMPORT_NOTE_TOO_LONG", "Import note is too long"),
                    "PRODUCT_INVALID": (404, "PRODUCT_NOT_FOUND", "Product is not available for warehouse import"),
                    "PRODUCT_HIDDEN": (409, "PRODUCT_HIDDEN", "Product is hidden"),
                    "IMPORT_NOTE_NOT_FOUND": (404, "IMPORT_NOTE_NOT_FOUND", "Import note does not exist"),
                }
                status, code, message = error_map.get(str(exc), (400, "IMPORT_INVALID", "Inventory import was rejected"))
                raise warehouse_error(status, code, message)
            payload = _result_payload(existing.id, result)
            existing.status = "completed"
            existing.accepted_count = result.accepted_count
            existing.duplicate_count = result.duplicate_count
            existing.response_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            await session.commit()
            return payload

    return router
