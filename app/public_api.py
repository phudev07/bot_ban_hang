import hashlib
import hmac
import ipaddress
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.lehai_suppliers import LeHaiPremiumClient
from app.models import (
    ApiClient,
    ApiOrderRequest,
    InventoryItem,
    Order,
    Product,
    User,
)
from app.partner_services import api_signature
from app.services import active_products, available_stock, purchase_product
from app.suppliers import SumistoreClient
from app.suppliers import EXTERNAL_FULFILLMENT_SOURCES
from app.utils import SecretCipher


templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
logger = logging.getLogger(__name__)
API_PROCESSING_RECOVERY_AFTER = timedelta(minutes=15)


CLOUDFLARE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "173.245.48.0/20",
        "103.21.244.0/22",
        "103.22.200.0/22",
        "103.31.4.0/22",
        "141.101.64.0/18",
        "108.162.192.0/18",
        "190.93.240.0/20",
        "188.114.96.0/20",
        "197.234.240.0/22",
        "198.41.128.0/17",
        "162.158.0.0/15",
        "104.16.0.0/13",
        "104.24.0.0/14",
        "172.64.0.0/13",
        "131.0.72.0/22",
        "2400:cb00::/32",
        "2606:4700::/32",
        "2803:f800::/32",
        "2405:b500::/32",
        "2405:8100::/32",
        "2a06:98c0::/29",
        "2c0f:f248::/32",
    )
)


class ApiOrderBody(BaseModel):
    product_id: int
    quantity: int = Field(default=1, ge=1, le=100)
    coupon_code: str | None = Field(default=None, max_length=64)


@dataclass(frozen=True)
class ApiPrincipal:
    client: ApiClient
    user: User


def api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def request_path(request: Request) -> str:
    return request.url.path + (f"?{request.url.query}" if request.url.query else "")


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    cloudflare_ip = request.headers.get("cf-connecting-ip", "").strip()
    if forwarded and cloudflare_ip:
        try:
            proxy_address = ipaddress.ip_address(forwarded)
            client_address = ipaddress.ip_address(cloudflare_ip)
        except ValueError:
            pass
        else:
            if any(proxy_address in network for network in CLOUDFLARE_NETWORKS):
                return str(client_address)
    return forwarded or (request.client.host if request.client else "")


def api_docs_url(settings: Settings) -> str:
    base_url = settings.shop_api_base_url.rstrip("/")
    origin = base_url.removesuffix("/v1")
    return f"{origin}/docs"


def create_public_api_docs_router(settings: Settings) -> APIRouter:
    router = APIRouter(tags=["shop-api-docs"])

    @router.get("/docs", response_class=HTMLResponse)
    @router.get("/docs/", response_class=HTMLResponse, include_in_schema=False)
    async def api_docs(request: Request) -> HTMLResponse:
        response = templates.TemplateResponse(
            request,
            "api_docs.html",
            {
                "base_url": settings.shop_api_base_url.rstrip("/"),
                "default_rate_limit": settings.shop_api_rate_limit_per_minute,
                "signature_tolerance_seconds": settings.shop_api_signature_tolerance_seconds,
            },
        )
        response.headers["Cache-Control"] = "public, max-age=300"
        return response

    @router.get("/v1/docs", include_in_schema=False)
    async def api_docs_redirect() -> RedirectResponse:
        return RedirectResponse("/docs", status_code=307)

    return router


def order_payload(orders: list[Order], cipher: SecretCipher) -> dict[str, object]:
    representative = orders[0]
    return {
        "order_code": representative.shop_order_code,
        "status": representative.status,
        "channel": representative.sales_channel,
        "product": {
            "id": representative.product.id,
            "name": representative.product.name_vi,
        },
        "quantity": len(orders),
        "total_amount": sum(order.amount for order in orders),
        "discount_amount": sum(order.discount_amount for order in orders),
        "accounts": [
            cipher.decrypt(order.inventory_item.encrypted_secret) for order in orders
        ],
        "created_at": representative.created_at.isoformat(),
        "delivered_at": (
            representative.delivered_at.isoformat() if representative.delivered_at else None
        ),
    }


async def orders_for_request(
    session: AsyncSession,
    api_client_id: int,
    request_id: int,
) -> list[Order]:
    return list(
        await session.scalars(
            select(Order)
            .where(
                Order.api_client_id == api_client_id,
                Order.api_order_request_id == request_id,
            )
            .options(selectinload(Order.product), selectinload(Order.inventory_item))
            .order_by(Order.id)
        )
    )


def create_public_api_router(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    cipher: SecretCipher,
    supplier_client: SumistoreClient | None,
    redis_client: Redis,
    *,
    lehai_client: LeHaiPremiumClient | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["shop-api"])

    async def authenticate(
        request: Request,
        x_shop_api_id: str = Header(default="", alias="X-Shop-API-ID"),
        x_timestamp: str = Header(default="", alias="X-Timestamp"),
        x_nonce: str = Header(default="", alias="X-Nonce"),
        x_signature: str = Header(default="", alias="X-Signature"),
    ) -> ApiPrincipal:
        if not settings.shop_api_enabled:
            raise api_error(503, "API_DISABLED", "Shop API is disabled")
        if not all((x_shop_api_id, x_timestamp, x_nonce, x_signature)):
            raise api_error(401, "AUTH_REQUIRED", "Missing API authentication headers")
        current_timestamp = int(time.time())
        try:
            timestamp = int(x_timestamp)
        except ValueError as exc:
            raise api_error(401, "INVALID_TIMESTAMP", "Invalid timestamp") from exc
        if abs(current_timestamp - timestamp) > settings.shop_api_signature_tolerance_seconds:
            raise api_error(401, "EXPIRED_REQUEST", "Request timestamp is outside the allowed window")
        if len(x_nonce) < 12 or len(x_nonce) > 128:
            raise api_error(401, "INVALID_NONCE", "Nonce must contain 12-128 characters")

        async with session_factory() as session:
            row = (
                await session.execute(
                    select(ApiClient, User)
                    .join(User, User.telegram_id == ApiClient.owner_user_id)
                    .where(ApiClient.api_id == x_shop_api_id)
                )
            ).one_or_none()
            if row is None:
                raise api_error(401, "INVALID_API_ID", "API client does not exist")
            api_client, user = row
            if not api_client.active or api_client.admin_blocked or user.is_blocked:
                raise api_error(403, "API_CLIENT_BLOCKED", "API client is disabled")
            remote_ip = client_ip(request)
            allowed_ips = {
                value.strip()
                for value in api_client.allowed_ips.replace("\n", ",").split(",")
                if value.strip()
            }
            if allowed_ips and remote_ip not in allowed_ips:
                raise api_error(403, "IP_NOT_ALLOWED", "Client IP is not allowed")
            body = await request.body()
            expected = api_signature(
                cipher.decrypt(api_client.encrypted_secret),
                x_timestamp,
                x_nonce,
                request.method,
                request_path(request),
                body,
            )
            if not hmac.compare_digest(expected, x_signature.lower()):
                raise api_error(401, "INVALID_SIGNATURE", "Request signature is invalid")
            try:
                nonce_added = await redis_client.set(
                    f"shop-api:nonce:{api_client.id}:{x_nonce}",
                    "1",
                    ex=settings.shop_api_signature_tolerance_seconds,
                    nx=True,
                )
                if not nonce_added:
                    raise api_error(409, "REPLAYED_REQUEST", "Nonce has already been used")
                # Use server time so clients cannot spread requests over
                # multiple buckets with different valid request timestamps.
                minute = current_timestamp // 60
                rate_key = f"shop-api:rate:{api_client.id}:{minute}"
                current_rate = await redis_client.incr(rate_key)
                if current_rate == 1:
                    await redis_client.expire(rate_key, 90)
                if current_rate > api_client.rate_limit_per_minute:
                    raise api_error(429, "RATE_LIMITED", "Too many requests")
            except HTTPException:
                raise
            except RedisError as exc:
                raise api_error(503, "AUTH_STORE_UNAVAILABLE", "API authentication is unavailable") from exc
            now = datetime.now(UTC)
            last_used_at = api_client.last_used_at
            if last_used_at is not None and last_used_at.tzinfo is None:
                last_used_at = last_used_at.replace(tzinfo=UTC)
            if last_used_at is None or last_used_at <= now - timedelta(minutes=1):
                api_client.last_used_at = now
                await session.commit()
            request.state.api_client_id = api_client.id
            return ApiPrincipal(api_client, user)

    @router.get("")
    async def api_information() -> dict[str, object]:
        return {
            "name": "VietShare Warehouse API",
            "version": "v1",
            "purpose": "Synchronize account products, selling prices and stock, then place orders",
            "currency": "VND",
            "documentation": api_docs_url(settings),
            "authentication": {
                "headers": [
                    "X-Shop-API-ID",
                    "X-Timestamp",
                    "X-Nonce",
                    "X-Signature",
                ],
                "signature": "HMAC-SHA256(timestamp|nonce|METHOD|PATH_WITH_QUERY|sha256(body))",
            },
            "endpoints": [
                "GET /v1/account",
                "GET /v1/products",
                "GET /v1/products/{product_id}",
                "GET /v1/stock/{product_id}",
                "POST /v1/orders",
                "GET /v1/orders",
                "GET /v1/orders/{order_code}",
            ],
        }

    @router.get("/health")
    async def api_health() -> dict[str, str]:
        return {"status": "ok", "service": "shop-api"}

    @router.get("/account")
    async def account(principal: ApiPrincipal = Depends(authenticate)) -> dict[str, object]:
        return {
            "api_id": principal.client.api_id,
            "active": principal.client.active,
            "admin_blocked": principal.client.admin_blocked,
            "telegram_id": principal.user.telegram_id,
            "balance": principal.user.balance,
            "rate_limit_per_minute": principal.client.rate_limit_per_minute,
        }

    @router.get("/catalog")
    @router.get("/products")
    async def products(principal: ApiPrincipal = Depends(authenticate)) -> dict[str, object]:
        async with session_factory() as session:
            rows = await active_products(session)
            local_stock = {
                int(product_id): int(stock)
                for product_id, stock in await session.execute(
                    select(InventoryItem.product_id, func.count(InventoryItem.id))
                    .where(InventoryItem.status == "available")
                    .group_by(InventoryItem.product_id)
                )
            }
            values = []
            for product in rows:
                values.append(
                    {
                        "id": product.id,
                        "name": product.name_vi,
                        "description": product.description_vi,
                        "price": product.price,
                        "stock": (
                            max(0, product.external_stock)
                            if product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES
                            else local_stock.get(product.id, 0)
                        ),
                        "allow_quantity": product.allow_quantity,
                        "max_quantity": product.max_quantity,
                    }
                )
        return {"count": len(values), "products": values}

    @router.get("/products/{product_id}")
    @router.get("/stock/{product_id}")
    async def product_stock(
        product_id: int,
        principal: ApiPrincipal = Depends(authenticate),
    ) -> dict[str, object]:
        async with session_factory() as session:
            product = await session.get(Product, product_id)
            if product is None or not product.active or product.product_type != "account":
                raise api_error(404, "PRODUCT_NOT_FOUND", "Product does not exist")
            stock = await available_stock(session, product.id)
            return {
                "id": product.id,
                "name": product.name_vi,
                "description": product.description_vi,
                "price": product.price,
                "stock": stock,
                "allow_quantity": product.allow_quantity,
                "max_quantity": product.max_quantity,
            }

    @router.post("/orders")
    async def create_order(
        body: ApiOrderBody,
        principal: ApiPrincipal = Depends(authenticate),
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    ) -> dict[str, object]:
        normalized_key = idempotency_key.strip()
        if not 8 <= len(normalized_key) <= 128:
            raise api_error(400, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key is required")
        request_json = json.dumps(body.model_dump(), sort_keys=True, separators=(",", ":"))
        request_hash = hashlib.sha256(request_json.encode()).hexdigest()
        order_request = ApiOrderRequest(
            api_client_id=principal.client.id,
            idempotency_key=normalized_key,
            request_hash=request_hash,
        )
        async with session_factory() as session:
            session.add(order_request)
            try:
                await session.commit()
                await session.refresh(order_request)
                claimed = True
            except IntegrityError:
                await session.rollback()
                claimed = False
                order_request = await session.scalar(
                    select(ApiOrderRequest).where(
                        ApiOrderRequest.api_client_id == principal.client.id,
                        ApiOrderRequest.idempotency_key == normalized_key,
                    ).with_for_update()
                )
                if order_request is None:
                    raise api_error(409, "IDEMPOTENCY_CONFLICT", "Could not claim request")
                if order_request.request_hash != request_hash:
                    raise api_error(
                        409,
                        "IDEMPOTENCY_MISMATCH",
                        "Idempotency-Key was already used with another request",
                    )
                existing_orders = await orders_for_request(
                    session,
                    principal.client.id,
                    order_request.id,
                )
                if existing_orders:
                    return {"success": True, "order": order_payload(existing_orders, cipher)}
                if order_request.status == "review":
                    order_request.status = "processing"
                    order_request.error_code = None
                    await session.commit()
                    claimed = True
                elif order_request.status == "processing":
                    updated_at = order_request.updated_at or order_request.created_at
                    if updated_at is None:
                        updated_at = datetime.min.replace(tzinfo=UTC)
                    elif updated_at.tzinfo is None:
                        updated_at = updated_at.replace(tzinfo=UTC)
                    if updated_at <= datetime.now(UTC) - API_PROCESSING_RECOVERY_AFTER:
                        # A worker can die after claiming the key but before
                        # creating orders. Let the partner safely retry the
                        # same idempotency key after the recovery window.
                        order_request.error_code = "STALE_REQUEST_RECOVERED"
                        await session.commit()
                        claimed = True
                    else:
                        raise api_error(
                            409,
                            "REQUEST_IN_PROGRESS",
                            "The original request is processing",
                        )
                elif order_request.status == "failed":
                    raise api_error(
                        409,
                        order_request.error_code or "PREVIOUS_REQUEST_FAILED",
                        "The original request failed",
                    )
                else:
                    raise api_error(409, "REQUEST_IN_PROGRESS", "The original request is processing")

        if not claimed:
            raise api_error(409, "REQUEST_IN_PROGRESS", "The original request is processing")
        try:
            result = await purchase_product(
                session_factory,
                principal.user.telegram_id,
                body.product_id,
                cipher,
                body.quantity,
                supplier_client,
                lehai_client=lehai_client,
                coupon_code=body.coupon_code,
                sales_channel="api",
                api_client_id=principal.client.id,
                api_order_request_id=order_request.id,
                referral_commission_percent=settings.referral_commission_percent,
                supplier_idempotency_key=(
                    f"shop-api-{principal.client.id}-{order_request.id}"
                ),
            )
        except Exception:
            logger.exception("Shop API order %s needs supplier review after an exception", order_request.id)
            async with session_factory() as session:
                stored_request = await session.get(ApiOrderRequest, order_request.id)
                if stored_request is not None:
                    stored_request.status = "review"
                    stored_request.error_code = "ORDER_STATE_REVIEW"
                    await session.commit()
            return JSONResponse(
                status_code=202,
                content={
                    "success": False,
                    "status": "review",
                    "request_id": order_request.id,
                    "message": (
                        "The order needs supplier review. Retry the same request with the "
                        "same Idempotency-Key; do not create a new key."
                    ),
                },
            )
        async with session_factory() as session:
            stored_request = await session.get(ApiOrderRequest, order_request.id)
            if stored_request is None:
                raise api_error(500, "ORDER_STATE_MISSING", "Order request state is missing")
            if not result.ok:
                if result.message == "supplier_unavailable":
                    stored_request.status = "review"
                    stored_request.error_code = "SUPPLIER_REVIEW"
                    await session.commit()
                    return JSONResponse(
                        status_code=202,
                        content={
                            "success": False,
                            "status": "review",
                            "request_id": order_request.id,
                            "message": (
                                "The supplier result is unclear. Retry the same request with "
                                "the same Idempotency-Key; do not create a new key."
                            ),
                        },
                    )
                stored_request.status = "failed"
                stored_request.error_code = result.message
                await session.commit()
                status_map = {
                    "insufficient": 402,
                    "blocked": 403,
                    "out_of_stock": 409,
                    "supplier_unavailable": 503,
                    "invalid_coupon": 400,
                    "invalid_quantity": 400,
                    "not_found": 404,
                }
                raise api_error(
                    status_map.get(result.message, 400),
                    result.message.upper(),
                    "Order could not be completed",
                )
            stored_request.status = "completed"
            stored_request.shop_order_code = result.orders[0].shop_order_code
            await session.commit()
        return {
            "success": True,
            "order": {
                "order_code": result.orders[0].shop_order_code,
                "status": "completed",
                "channel": "api",
                "product": {
                    "id": result.orders[0].product.id,
                    "name": result.orders[0].product.name_vi,
                },
                "quantity": len(result.orders),
                "total_amount": result.total_amount,
                "discount_amount": result.discount_amount,
                "accounts": result.secrets,
                "created_at": result.orders[0].created_at.isoformat(),
                "delivered_at": result.orders[0].delivered_at.isoformat(),
            },
        }

    @router.get("/orders")
    async def list_orders(
        limit: int = 20,
        principal: ApiPrincipal = Depends(authenticate),
    ) -> dict[str, object]:
        normalized_limit = max(1, min(limit, 100))
        async with session_factory() as session:
            requests = list(
                await session.scalars(
                    select(ApiOrderRequest)
                    .where(
                        ApiOrderRequest.api_client_id == principal.client.id,
                        ApiOrderRequest.status == "completed",
                    )
                    .order_by(ApiOrderRequest.id.desc())
                    .limit(normalized_limit)
                )
            )
            request_ids = [order_request.id for order_request in requests]
            grouped_orders: dict[int, list[Order]] = {request_id: [] for request_id in request_ids}
            if request_ids:
                order_rows = await session.scalars(
                    select(Order)
                    .where(
                        Order.api_client_id == principal.client.id,
                        Order.api_order_request_id.in_(request_ids),
                    )
                    .options(selectinload(Order.product), selectinload(Order.inventory_item))
                    .order_by(Order.id)
                )
                for order in order_rows:
                    if order.api_order_request_id is not None:
                        grouped_orders[order.api_order_request_id].append(order)
            values = [
                order_payload(grouped_orders[order_request.id], cipher)
                for order_request in requests
                if grouped_orders[order_request.id]
            ]
        return {"count": len(values), "orders": values}

    @router.get("/orders/{order_code}")
    async def get_order(
        order_code: str,
        principal: ApiPrincipal = Depends(authenticate),
    ) -> dict[str, object]:
        async with session_factory() as session:
            order_request = await session.scalar(
                select(ApiOrderRequest).where(
                    ApiOrderRequest.api_client_id == principal.client.id,
                    ApiOrderRequest.shop_order_code == order_code,
                    ApiOrderRequest.status == "completed",
                )
            )
            if order_request is None:
                raise api_error(404, "ORDER_NOT_FOUND", "Order does not exist")
            orders = await orders_for_request(session, principal.client.id, order_request.id)
            if not orders:
                raise api_error(404, "ORDER_NOT_FOUND", "Order does not exist")
            return {"success": True, "order": order_payload(orders, cipher)}

    return router
