import asyncio
import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx
from aiogram import Bot
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.delivery import (
    codex_auth_content,
    codex_config_content,
    codex_setup_keyboard,
    codex_setup_text,
    router_token_delivery_text,
)
from app.keyboards import router_token_delivery_keyboard
from app.models import (
    Category,
    Deposit,
    DiscountCode,
    InventoryItem,
    Order,
    Product,
    RouterCapacityState,
    RouterTokenPurchase,
    User,
)
from app.utils import SecretCipher


logger = logging.getLogger(__name__)
ROUTER_PRODUCT_CODE = "9router-gpt-token"
ROUTER_PAID_STATUSES = ("pending", "provisioning", "retry", "fulfilled")


class RouterTokenError(RuntimeError):
    pass


class RouterTokenKeyNotFound(RouterTokenError):
    pass


@dataclass(frozen=True)
class RouterProvisionedKey:
    key_id: str
    key: str
    token_quota: int
    tokens_used: int
    remaining_tokens: int
    is_active: bool
    disabled_reason: str | None = None


@dataclass(frozen=True)
class RouterCapacityUsage:
    key_count: int
    active_keys: int
    exhausted_keys: int
    token_quota: int
    tokens_used: int
    reserved_tokens: int
    remaining_tokens: int
    available_tokens: int


@dataclass(frozen=True)
class RouterUsageLog:
    timestamp: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    status: str


@dataclass(frozen=True)
class RouterUsageModel:
    model: str
    requests: int
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class RouterTokenUsage:
    shop_order_id: str
    key_id: str
    token_quota: int
    tokens_used: int
    reserved_tokens: int
    remaining_tokens: int
    available_tokens: int
    is_active: bool
    disabled_reason: str | None
    created_at: str | None
    updated_at: str | None
    total_requests: int
    input_tokens: int
    output_tokens: int
    total_log_tokens: int
    first_request_at: str | None
    last_request_at: str | None
    models: tuple[RouterUsageModel, ...]
    logs: tuple[RouterUsageLog, ...]

    def public_payload(self) -> dict[str, object]:
        return {
            "shop_order_id": self.shop_order_id,
            "key_id": self.key_id,
            "token_quota": self.token_quota,
            "tokens_used": self.tokens_used,
            "reserved_tokens": self.reserved_tokens,
            "remaining_tokens": self.remaining_tokens,
            "available_tokens": self.available_tokens,
            "is_active": self.is_active,
            "disabled_reason": self.disabled_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "total_requests": self.total_requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_log_tokens": self.total_log_tokens,
            "first_request_at": self.first_request_at,
            "last_request_at": self.last_request_at,
            "models": [
                {
                    "model": item.model,
                    "requests": item.requests,
                    "input_tokens": item.input_tokens,
                    "output_tokens": item.output_tokens,
                    "total_tokens": item.total_tokens,
                }
                for item in self.models
            ],
            "logs": [
                {
                    "timestamp": item.timestamp,
                    "model": item.model,
                    "input_tokens": item.input_tokens,
                    "output_tokens": item.output_tokens,
                    "total_tokens": item.total_tokens,
                    "status": item.status,
                }
                for item in self.logs
            ],
        }


class RouterTokenClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.router_base_url.rstrip("/")
        self.secret = settings.router_hmac_secret.get_secret_value().encode()
        self.public_api_url = settings.router_public_api_url.rstrip("/")
        self.usage_page_url = settings.token_usage_url
        self.allowed_models = list(settings.router_allowed_models)
        self.client = httpx.AsyncClient(timeout=settings.router_timeout_seconds)

    def _headers(self, method: str, path: str, raw_body: str = "") -> dict[str, str]:
        timestamp = str(int(datetime.now(UTC).timestamp()))
        canonical = f"{timestamp}\n{method.upper()}\n{path}\n{raw_body}".encode()
        signature = hmac.new(self.secret, canonical, hashlib.sha256).hexdigest()
        return {
            "Content-Type": "application/json",
            "X-Shop-Timestamp": timestamp,
            "X-Shop-Signature": signature,
        }

    @staticmethod
    def _parse_key(payload: dict[str, object]) -> RouterProvisionedKey:
        key = str(payload.get("key") or "")
        if not key.startswith("sk-"):
            raise RouterTokenError("9Router returned an invalid API key")
        return RouterProvisionedKey(
            key_id=str(payload.get("keyId") or ""),
            key=key,
            token_quota=int(payload.get("tokenQuota") or 0),
            tokens_used=int(payload.get("tokensUsed") or 0),
            remaining_tokens=int(payload.get("remainingTokens") or 0),
            is_active=bool(payload.get("isActive")),
            disabled_reason=(
                str(payload.get("disabledReason")) if payload.get("disabledReason") else None
            ),
        )

    async def provision(
        self,
        *,
        shop_order_id: str,
        telegram_user_id: int,
        token_quota: int,
    ) -> RouterProvisionedKey:
        path = "/api/internal/shop/keys"
        raw_body = json.dumps(
            {
                "shopOrderId": shop_order_id,
                "telegramUserId": str(telegram_user_id),
                "tokenQuota": token_quota,
                "allowedModels": self.allowed_models,
                "name": f"Telegram {telegram_user_id} - {shop_order_id}",
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        try:
            response = await self.client.post(
                f"{self.base_url}{path}",
                content=raw_body,
                headers=self._headers("POST", path, raw_body),
            )
            response.raise_for_status()
            return self._parse_key(response.json())
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise RouterTokenError(f"Could not provision 9Router key: {exc}") from exc

    async def status(self, shop_order_id: str) -> RouterProvisionedKey:
        encoded_order_id = quote(shop_order_id, safe="")
        path = f"/api/internal/shop/keys/{encoded_order_id}"
        try:
            response = await self.client.get(
                f"{self.base_url}{path}",
                headers=self._headers("GET", path),
            )
            response.raise_for_status()
            return self._parse_key(response.json())
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise RouterTokenError(f"Could not read 9Router key status: {exc}") from exc

    @staticmethod
    def _parse_usage(payload: dict[str, object]) -> RouterTokenUsage:
        stats = payload.get("stats")
        stats = stats if isinstance(stats, dict) else {}
        raw_models = stats.get("models")
        raw_logs = payload.get("logs")

        models = tuple(
            RouterUsageModel(
                model=str(item.get("model") or "unknown"),
                requests=int(item.get("requests") or 0),
                input_tokens=int(item.get("inputTokens") or 0),
                output_tokens=int(item.get("outputTokens") or 0),
                total_tokens=int(item.get("totalTokens") or 0),
            )
            for item in (raw_models if isinstance(raw_models, list) else [])
            if isinstance(item, dict)
        )
        logs = tuple(
            RouterUsageLog(
                timestamp=str(item.get("timestamp") or ""),
                model=str(item.get("model") or "unknown"),
                input_tokens=int(item.get("inputTokens") or 0),
                output_tokens=int(item.get("outputTokens") or 0),
                total_tokens=int(item.get("totalTokens") or 0),
                status=str(item.get("status") or "unknown"),
            )
            for item in (raw_logs if isinstance(raw_logs, list) else [])
            if isinstance(item, dict)
        )
        return RouterTokenUsage(
            shop_order_id=str(payload.get("shopOrderId") or ""),
            key_id=str(payload.get("keyId") or ""),
            token_quota=int(payload.get("tokenQuota") or 0),
            tokens_used=int(payload.get("tokensUsed") or 0),
            reserved_tokens=int(payload.get("reservedTokens") or 0),
            remaining_tokens=int(payload.get("remainingTokens") or 0),
            available_tokens=int(payload.get("availableTokens") or 0),
            is_active=bool(payload.get("isActive")),
            disabled_reason=(
                str(payload.get("disabledReason")) if payload.get("disabledReason") else None
            ),
            created_at=str(payload.get("createdAt")) if payload.get("createdAt") else None,
            updated_at=str(payload.get("updatedAt")) if payload.get("updatedAt") else None,
            total_requests=int(stats.get("totalRequests") or 0),
            input_tokens=int(stats.get("inputTokens") or 0),
            output_tokens=int(stats.get("outputTokens") or 0),
            total_log_tokens=int(stats.get("totalTokens") or 0),
            first_request_at=(
                str(stats.get("firstRequestAt")) if stats.get("firstRequestAt") else None
            ),
            last_request_at=(
                str(stats.get("lastRequestAt")) if stats.get("lastRequestAt") else None
            ),
            models=models,
            logs=logs,
        )

    async def usage(self, api_key: str) -> RouterTokenUsage:
        path = "/api/internal/shop/usage"
        raw_body = json.dumps(
            {"apiKey": api_key},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        try:
            response = await self.client.post(
                f"{self.base_url}{path}",
                content=raw_body,
                headers=self._headers("POST", path, raw_body),
            )
            if response.status_code == 404:
                raise RouterTokenKeyNotFound("Token key was not found")
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RouterTokenError("9Router returned an invalid usage payload")
            return self._parse_usage(payload)
        except RouterTokenKeyNotFound:
            raise
        except RouterTokenError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise RouterTokenError(f"Could not read 9Router token usage: {exc}") from exc

    async def capacity(self) -> RouterCapacityUsage:
        path = "/api/internal/shop/capacity"
        try:
            response = await self.client.get(
                f"{self.base_url}{path}",
                headers=self._headers("GET", path),
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RouterTokenError("9Router returned an invalid capacity payload")
            return RouterCapacityUsage(
                key_count=int(payload.get("keyCount") or 0),
                active_keys=int(payload.get("activeKeys") or 0),
                exhausted_keys=int(payload.get("exhaustedKeys") or 0),
                token_quota=int(payload.get("tokenQuota") or 0),
                tokens_used=int(payload.get("tokensUsed") or 0),
                reserved_tokens=int(payload.get("reservedTokens") or 0),
                remaining_tokens=int(payload.get("remainingTokens") or 0),
                available_tokens=int(payload.get("availableTokens") or 0),
            )
        except RouterTokenError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise RouterTokenError(f"Could not read 9Router capacity: {exc}") from exc

    async def close(self) -> None:
        await self.client.aclose()


def create_router_token_client(settings: Settings) -> RouterTokenClient | None:
    return RouterTokenClient(settings) if settings.router_tokens_enabled else None


def token_quota_for_amount(amount: int, settings: Settings) -> int:
    return amount * settings.router_tokens_per_vnd


@dataclass(frozen=True)
class RouterCapacitySnapshot:
    total_capacity_tokens: int
    issued_quota_tokens: int
    used_tokens: int
    outstanding_tokens: int
    available_tokens: int
    sellable_tokens: int
    active_keys: int
    failed_keys: int
    status: str
    last_error: str | None
    checked_at: datetime


async def refresh_router_capacity(
    session_factory: async_sessionmaker[AsyncSession],
    client: RouterTokenClient,
    settings: Settings,
) -> RouterCapacitySnapshot:
    async with session_factory() as session:
        purchases = list(
            (
                await session.execute(
                    select(
                        RouterTokenPurchase.shop_order_id,
                        RouterTokenPurchase.token_quota,
                        RouterTokenPurchase.status,
                    ).where(RouterTokenPurchase.status.in_(ROUTER_PAID_STATUSES))
                )
            ).all()
        )

    pending_tokens = sum(
        max(0, int(row.token_quota)) for row in purchases if row.status != "fulfilled"
    )
    fallback_issued = sum(max(0, int(row.token_quota)) for row in purchases)
    try:
        usage = await client.capacity()
    except RouterTokenError as exc:
        issued_quota = fallback_issued
        used_tokens = 0
        outstanding_tokens = fallback_issued
        active_keys = 0
        failed_keys = sum(1 for row in purchases if row.status == "fulfilled")
        last_error = str(exc)[:300]
    else:
        issued_quota = max(0, usage.token_quota) + pending_tokens
        used_tokens = max(0, usage.tokens_used)
        outstanding_tokens = max(0, usage.remaining_tokens) + pending_tokens
        active_keys = max(0, usage.active_keys)
        failed_keys = 0
        last_error = None

    total_capacity = max(0, settings.router_capacity_tokens)
    available_tokens = max(0, total_capacity - outstanding_tokens) if total_capacity else 0
    sellable_tokens = (
        max(0, available_tokens - settings.router_capacity_reserve_tokens)
        if total_capacity
        else 0
    )
    minimum_package = settings.router_min_purchase * settings.router_tokens_per_vnd
    if last_error:
        status = "degraded"
    elif total_capacity == 0:
        status = "monitoring"
    elif available_tokens == 0:
        status = "depleted"
    elif sellable_tokens < minimum_package:
        status = "low"
    else:
        status = "healthy"
    checked_at = datetime.now(UTC)

    async with session_factory() as session:
        async with session.begin():
            state = await session.scalar(
                select(RouterCapacityState)
                .where(RouterCapacityState.id == 1)
                .with_for_update()
            )
            if state is None:
                state = RouterCapacityState(id=1)
                session.add(state)
            state.total_capacity_tokens = total_capacity
            state.issued_quota_tokens = issued_quota
            state.used_tokens = used_tokens
            state.outstanding_tokens = outstanding_tokens
            state.available_tokens = available_tokens
            state.active_keys = active_keys
            state.failed_keys = failed_keys
            state.status = status
            state.last_error = last_error
            state.checked_at = checked_at
            if status not in {"low", "depleted"}:
                state.low_notified_at = None

    return RouterCapacitySnapshot(
        total_capacity_tokens=total_capacity,
        issued_quota_tokens=issued_quota,
        used_tokens=used_tokens,
        outstanding_tokens=outstanding_tokens,
        available_tokens=available_tokens,
        sellable_tokens=sellable_tokens,
        active_keys=active_keys,
        failed_keys=failed_keys,
        status=status,
        last_error=last_error,
        checked_at=checked_at,
    )


async def claim_router_capacity(
    session: AsyncSession,
    *,
    requested_tokens: int,
    total_capacity_tokens: int,
    reserve_tokens: int,
    sync_seconds: int,
    claim: bool,
) -> bool:
    if total_capacity_tokens <= 0:
        return True
    state = await session.scalar(
        select(RouterCapacityState)
        .where(RouterCapacityState.id == 1)
        .with_for_update()
    )
    if state is None or state.checked_at is None:
        return False
    checked_at = _as_utc(state.checked_at)
    stale_after = timedelta(seconds=max(120, sync_seconds * 3))
    if checked_at is None or checked_at < datetime.now(UTC) - stale_after:
        return False
    if state.total_capacity_tokens != total_capacity_tokens:
        return False
    sellable_tokens = max(0, state.available_tokens - max(0, reserve_tokens))
    requested_tokens = max(0, requested_tokens)
    if requested_tokens > sellable_tokens:
        return False
    if claim:
        state.issued_quota_tokens += requested_tokens
        state.outstanding_tokens += requested_tokens
        state.available_tokens = max(0, state.available_tokens - requested_tokens)
        minimum_sellable = max(0, state.available_tokens - max(0, reserve_tokens))
        if minimum_sellable == 0:
            state.status = "low"
    return True


async def notify_low_router_capacity(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    settings: Settings,
    snapshot: RouterCapacitySnapshot,
) -> bool:
    if snapshot.status not in {"low", "depleted"} or not settings.admin_ids:
        return False
    now = datetime.now(UTC)
    async with session_factory() as session:
        async with session.begin():
            state = await session.scalar(
                select(RouterCapacityState)
                .where(RouterCapacityState.id == 1)
                .with_for_update()
            )
            if state is None:
                return False
            notified_at = _as_utc(state.low_notified_at)
            if notified_at and notified_at > now - timedelta(hours=6):
                return False
            state.low_notified_at = now

    message = (
        "⚠️ <b>Nguồn GPT token sắp cạn</b>\n\n"
        f"• Tổng sức chứa: <b>{snapshot.total_capacity_tokens:,}</b> token\n"
        f"• Khách còn có thể dùng: <b>{snapshot.outstanding_tokens:,}</b> token\n"
        f"• Có thể bán thêm: <b>{snapshot.sellable_tokens:,}</b> token\n"
        f"• Key đang hoạt động: <b>{snapshot.active_keys}</b>\n\n"
        "Hệ thống đã chặn đơn vượt sức chứa. Hãy bổ sung nguồn hợp lệ rồi cập nhật hạn mức."
    ).replace(",", ".")
    delivered = False
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, message)
            delivered = True
        except Exception:
            logger.exception("Could not notify admin %s about low router capacity", admin_id)
    return delivered


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def router_coupon_is_usable(coupon: DiscountCode | None, now: datetime | None = None) -> bool:
    current = now or datetime.now(UTC)
    return bool(
        coupon is not None
        and coupon.active
        and (_as_utc(coupon.starts_at) is None or _as_utc(coupon.starts_at) <= current)
        and (_as_utc(coupon.expires_at) is None or _as_utc(coupon.expires_at) > current)
        and (coupon.max_uses <= 0 or coupon.used_count < coupon.max_uses)
    )


@dataclass(frozen=True)
class RouterAmountPricing:
    face_amount: int
    paid_amount: int
    discount_amount: int
    coupon: DiscountCode | None = None


async def router_amount_pricing(
    session: AsyncSession,
    product: Product,
    face_amount: int,
    *,
    coupon_code: str | None = None,
    coupon_id: int | None = None,
    lock_coupon: bool = False,
) -> RouterAmountPricing | None:
    if not coupon_code and coupon_id is None:
        return RouterAmountPricing(face_amount, face_amount, 0)

    statement = select(DiscountCode).where(DiscountCode.product_id == product.id)
    if coupon_id is not None:
        statement = statement.where(DiscountCode.id == coupon_id)
    else:
        statement = statement.where(DiscountCode.code == (coupon_code or "").strip().upper())
    if lock_coupon:
        statement = statement.with_for_update()
    coupon = await session.scalar(statement)
    if not router_coupon_is_usable(coupon):
        return None

    if coupon.discount_type == "percent":
        discount = face_amount * coupon.discount_value // 100
    else:
        discount = coupon.discount_value
    discount = max(0, min(discount, max(0, face_amount - 1)))
    return RouterAmountPricing(face_amount, face_amount - discount, discount, coupon)


async def ensure_router_token_product(session_factory, settings: Settings) -> Product | None:
    if not settings.router_tokens_enabled:
        return None
    async with session_factory() as session:
        product = await session.scalar(
            select(Product).where(Product.supplier_product_id == ROUTER_PRODUCT_CODE)
        )
        if product is not None:
            return product
        category = await session.scalar(
            select(Category).where(Category.name_en == "LLM services").order_by(Category.id)
        )
        if category is None:
            category = Category(
                name_vi="Dịch vụ LLM",
                name_en="LLM services",
                position=2,
                active=True,
            )
            session.add(category)
            await session.flush()
        product = Product(
            category_id=category.id,
            name_vi="GPT Token 9Router",
            name_en="GPT Tokens via 9Router",
            description_vi=(
                "Nhập số tiền muốn mua, tối thiểu 10.000đ. Mỗi 10.000đ nhận "
                "10.000.000 token dùng chung input + output. Key tự khóa khi hết token."
            ),
            description_en=(
                "Enter a purchase amount from 10,000 VND. Every 10,000 VND grants "
                "10,000,000 combined input and output tokens. The key stops at zero."
            ),
            price=settings.router_min_purchase,
            product_type="token",
            allow_quantity=False,
            max_quantity=1,
            fulfillment_source="9router",
            supplier_product_id=ROUTER_PRODUCT_CODE,
            supplier_price=0,
            external_stock=0,
            active=True,
        )
        session.add(product)
        await session.commit()
        await session.refresh(product)
        return product


@dataclass
class RouterPurchaseResult:
    ok: bool
    message: str
    purchase_id: int | None = None
    paid_amount: int = 0
    discount_amount: int = 0
    token_quota: int = 0
    coupon_code: str | None = None


def _cost_snapshot(product: Product, face_amount: int, base_amount: int) -> int:
    base_cost = max(0, product.supplier_price or 0)
    return base_cost * face_amount // max(1, base_amount)


async def create_wallet_router_purchase(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    telegram_id: int,
    product_id: int,
    face_amount: int,
    *,
    coupon_id: int | None = None,
) -> RouterPurchaseResult:
    async with session_factory() as session:
        async with session.begin():
            user = await session.scalar(
                select(User).where(User.telegram_id == telegram_id).with_for_update()
            )
            product = await session.get(Product, product_id)
            if (
                user is None
                or product is None
                or not product.active
                or product.fulfillment_source != "9router"
            ):
                return RouterPurchaseResult(False, "not_found")
            if user.is_blocked:
                return RouterPurchaseResult(False, "blocked")
            if face_amount < settings.router_min_purchase:
                return RouterPurchaseResult(False, "invalid_amount")
            pricing = await router_amount_pricing(
                session,
                product,
                face_amount,
                coupon_id=coupon_id,
                lock_coupon=coupon_id is not None,
            )
            if pricing is None:
                return RouterPurchaseResult(False, "invalid_coupon")
            quota = token_quota_for_amount(face_amount, settings)
            if user.balance < pricing.paid_amount:
                return RouterPurchaseResult(
                    False,
                    "insufficient",
                    paid_amount=pricing.paid_amount,
                    discount_amount=pricing.discount_amount,
                    token_quota=quota,
                    coupon_code=pricing.coupon.code if pricing.coupon else None,
                )
            if not await claim_router_capacity(
                session,
                requested_tokens=quota,
                total_capacity_tokens=settings.router_capacity_tokens,
                reserve_tokens=settings.router_capacity_reserve_tokens,
                sync_seconds=settings.router_capacity_sync_seconds,
                claim=True,
            ):
                return RouterPurchaseResult(
                    False,
                    "capacity",
                    paid_amount=pricing.paid_amount,
                    discount_amount=pricing.discount_amount,
                    token_quota=quota,
                    coupon_code=pricing.coupon.code if pricing.coupon else None,
                )

            purchase = RouterTokenPurchase(
                shop_order_id=f"RT-{secrets.token_hex(12)}",
                user_id=user.telegram_id,
                product_id=product.id,
                source="wallet",
                face_amount=face_amount,
                paid_amount=pricing.paid_amount,
                cost_amount=_cost_snapshot(
                    product,
                    face_amount,
                    settings.router_min_purchase,
                ),
                token_quota=quota,
                discount_amount=pricing.discount_amount,
                discount_code_id=pricing.coupon.id if pricing.coupon else None,
                discount_code=pricing.coupon.code if pricing.coupon else None,
                status="pending",
                next_retry_at=datetime.now(UTC),
            )
            user.balance -= pricing.paid_amount
            if pricing.coupon is not None:
                pricing.coupon.used_count += 1
            session.add(purchase)
            await session.flush()
            return RouterPurchaseResult(
                True,
                "pending",
                purchase_id=purchase.id,
                paid_amount=pricing.paid_amount,
                discount_amount=pricing.discount_amount,
                token_quota=quota,
                coupon_code=pricing.coupon.code if pricing.coupon else None,
            )


async def create_qr_router_purchase(
    session: AsyncSession,
    settings: Settings,
    user: User,
    product: Product,
    face_amount: int,
    *,
    coupon_id: int | None = None,
) -> tuple[Deposit, RouterTokenPurchase] | None:
    if face_amount < settings.router_min_purchase or product.fulfillment_source != "9router":
        return None
    pricing = await router_amount_pricing(
        session,
        product,
        face_amount,
        coupon_id=coupon_id,
    )
    if pricing is None:
        return None
    deposit = Deposit(
        user_id=user.telegram_id,
        code=f"{settings.payment_prefix.upper()}{user.telegram_id}{secrets.token_hex(2).upper()}",
        requested_amount=pricing.paid_amount,
        payment_kind="router_token_purchase",
        product_id=product.id,
        quantity=1,
        discount_amount=pricing.discount_amount,
        discount_code_id=pricing.coupon.id if pricing.coupon else None,
        discount_code=pricing.coupon.code if pricing.coupon else None,
    )
    session.add(deposit)
    await session.flush()
    purchase = RouterTokenPurchase(
        shop_order_id=f"RT-{secrets.token_hex(12)}",
        user_id=user.telegram_id,
        product_id=product.id,
        deposit_id=deposit.id,
        source="qr",
        face_amount=face_amount,
        paid_amount=pricing.paid_amount,
        cost_amount=_cost_snapshot(
            product,
            face_amount,
            settings.router_min_purchase,
        ),
        token_quota=token_quota_for_amount(face_amount, settings),
        discount_amount=pricing.discount_amount,
        discount_code_id=pricing.coupon.id if pricing.coupon else None,
        discount_code=pricing.coupon.code if pricing.coupon else None,
        status="awaiting_payment",
    )
    session.add(purchase)
    await session.commit()
    await session.refresh(deposit)
    await session.refresh(purchase)
    return deposit, purchase


@dataclass(frozen=True)
class RouterFulfillmentResult:
    ok: bool
    purchase_id: int
    order_id: int | None = None
    message: str = ""


async def fulfill_router_token_purchase(
    session_factory: async_sessionmaker[AsyncSession],
    client: RouterTokenClient,
    cipher: SecretCipher,
    purchase_id: int,
) -> RouterFulfillmentResult:
    async with session_factory() as session:
        async with session.begin():
            purchase = await session.scalar(
                select(RouterTokenPurchase)
                .where(RouterTokenPurchase.id == purchase_id)
                .with_for_update()
            )
            if purchase is None:
                return RouterFulfillmentResult(False, purchase_id, message="not_found")
            if purchase.status == "fulfilled" and purchase.order_id is not None:
                return RouterFulfillmentResult(True, purchase.id, purchase.order_id, "fulfilled")
            if purchase.status not in {"pending", "retry", "provisioning"}:
                return RouterFulfillmentResult(False, purchase.id, message=purchase.status)
            now = datetime.now(UTC)
            if _as_utc(purchase.next_retry_at) and _as_utc(purchase.next_retry_at) > now:
                return RouterFulfillmentResult(False, purchase.id, message="not_due")
            purchase.status = "provisioning"
            purchase.attempt_count += 1
            purchase.next_retry_at = now + timedelta(seconds=60)
            shop_order_id = purchase.shop_order_id
            user_id = purchase.user_id
            token_quota = purchase.token_quota

    try:
        provisioned = await client.provision(
            shop_order_id=shop_order_id,
            telegram_user_id=user_id,
            token_quota=token_quota,
        )
    except RouterTokenError as exc:
        async with session_factory() as session:
            async with session.begin():
                purchase = await session.scalar(
                    select(RouterTokenPurchase)
                    .where(RouterTokenPurchase.id == purchase_id)
                    .with_for_update()
                )
                if purchase is not None and purchase.status != "fulfilled":
                    delay = min(3600, 15 * (2 ** min(purchase.attempt_count, 8)))
                    purchase.status = "retry"
                    purchase.next_retry_at = datetime.now(UTC) + timedelta(seconds=delay)
                    purchase.last_error = str(exc)[:500]
        logger.warning("9Router purchase %s will retry: %s", purchase_id, exc)
        return RouterFulfillmentResult(False, purchase_id, message="retry")

    async with session_factory() as session:
        async with session.begin():
            purchase = await session.scalar(
                select(RouterTokenPurchase)
                .where(RouterTokenPurchase.id == purchase_id)
                .with_for_update()
            )
            if purchase is None:
                return RouterFulfillmentResult(False, purchase_id, message="not_found")
            if purchase.order_id is not None:
                purchase.status = "fulfilled"
                return RouterFulfillmentResult(True, purchase.id, purchase.order_id, "fulfilled")

            now = datetime.now(UTC)
            item = InventoryItem(
                product_id=purchase.product_id,
                encrypted_secret=cipher.encrypt(provisioned.key),
                status="sold",
                sold_at=now,
            )
            session.add(item)
            await session.flush()
            order = Order(
                user_id=purchase.user_id,
                product_id=purchase.product_id,
                inventory_item_id=item.id,
                amount=purchase.paid_amount,
                cost_amount=purchase.cost_amount,
                discount_amount=purchase.discount_amount,
                discount_code_id=purchase.discount_code_id,
                discount_code=purchase.discount_code,
                batch_code=f"R{secrets.token_hex(5).upper()}",
                supplier_order_code=purchase.shop_order_id,
                status="completed",
                delivered_at=now,
            )
            session.add(order)
            await session.flush()
            purchase.order_id = order.id
            purchase.router_key_id = provisioned.key_id
            purchase.encrypted_key = item.encrypted_secret
            purchase.status = "fulfilled"
            purchase.next_retry_at = None
            purchase.last_error = None
            return RouterFulfillmentResult(True, purchase.id, order.id, "fulfilled")


async def notify_router_token_purchase(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    cipher: SecretCipher,
    public_api_url: str,
    purchase_id: int,
    usage_page_url: str = "",
) -> bool:
    now = datetime.now(UTC)
    stale_before = now - timedelta(minutes=5)
    async with session_factory() as session:
        async with session.begin():
            claimed_id = await session.scalar(
                update(RouterTokenPurchase)
                .where(
                    RouterTokenPurchase.id == purchase_id,
                    RouterTokenPurchase.status == "fulfilled",
                    RouterTokenPurchase.notified_at.is_(None),
                    RouterTokenPurchase.encrypted_key.is_not(None),
                    RouterTokenPurchase.order_id.is_not(None),
                    or_(
                        RouterTokenPurchase.notification_claimed_at.is_(None),
                        RouterTokenPurchase.notification_claimed_at <= stale_before,
                    ),
                )
                .values(notification_claimed_at=now)
                .returning(RouterTokenPurchase.id)
            )
            if claimed_id is None:
                return False
            purchase = await session.scalar(
                select(RouterTokenPurchase)
                .where(RouterTokenPurchase.id == purchase_id)
                .options(selectinload(RouterTokenPurchase.product))
            )
            if (
                purchase is None
                or not purchase.encrypted_key
                or purchase.order_id is None
            ):
                return False
            user = await session.get(User, purchase.user_id)
            if user is None:
                return False
            order = await session.get(Order, purchase.order_id)
            if order is None:
                return False
            key = cipher.decrypt(purchase.encrypted_key)
            language = user.language
            delivery = {
                "user_id": purchase.user_id,
                "order_id": purchase.order_id,
                "shop_order_code": order.shop_order_code,
                "product_name": (
                    purchase.product.name_en if language == "en" else purchase.product.name_vi
                ),
                "api_key": key,
                "token_quota": purchase.token_quota,
                "paid_amount": purchase.paid_amount,
                "language": language,
            }

    try:
        await bot.send_message(
            delivery["user_id"],
            router_token_delivery_text(
                shop_order_code=delivery["shop_order_code"],
                product_name=delivery["product_name"],
                api_url=public_api_url,
                api_key=delivery["api_key"],
                token_quota=delivery["token_quota"],
                paid_amount=delivery["paid_amount"],
                language=delivery["language"],
            ),
            reply_markup=router_token_delivery_keyboard(
                order_id=delivery["order_id"],
                api_key=delivery["api_key"],
                language=delivery["language"],
                usage_url=usage_page_url,
            ),
        )
        config_content = codex_config_content(public_api_url)
        await bot.send_message(
            delivery["user_id"],
            codex_setup_text(
                filename="~/.codex/config.toml",
                content=config_content,
                code_language="toml",
                step=1,
                language=delivery["language"],
            ),
            reply_markup=codex_setup_keyboard(
                filename="config.toml",
                content=config_content,
                language=delivery["language"],
            ),
        )
        auth_content = codex_auth_content(delivery["api_key"])
        await bot.send_message(
            delivery["user_id"],
            codex_setup_text(
                filename="~/.codex/auth.json",
                content=auth_content,
                code_language="json",
                step=2,
                language=delivery["language"],
            ),
            reply_markup=codex_setup_keyboard(
                filename="auth.json",
                content=auth_content,
                language=delivery["language"],
            ),
        )
    except Exception:
        logger.exception("Could not notify user for 9Router purchase %s", purchase_id)
        async with session_factory() as session:
            async with session.begin():
                purchase = await session.scalar(
                    select(RouterTokenPurchase)
                    .where(RouterTokenPurchase.id == purchase_id)
                    .with_for_update()
                )
                if purchase is not None and purchase.notified_at is None:
                    purchase.notification_claimed_at = None
        return False

    async with session_factory() as session:
        async with session.begin():
            purchase = await session.scalar(
                select(RouterTokenPurchase)
                .where(RouterTokenPurchase.id == purchase_id)
                .with_for_update()
            )
            if purchase is not None:
                purchase.notified_at = datetime.now(UTC)
                purchase.notification_claimed_at = None
    return True


async def router_token_worker(
    session_factory: async_sessionmaker[AsyncSession],
    client: RouterTokenClient,
    cipher: SecretCipher,
    bot: Bot,
    settings: Settings,
    interval_seconds: int = 5,
) -> None:
    next_capacity_refresh = 0.0
    while True:
        try:
            monotonic_now = asyncio.get_running_loop().time()
            if monotonic_now >= next_capacity_refresh:
                next_capacity_refresh = monotonic_now + settings.router_capacity_sync_seconds
                snapshot = await refresh_router_capacity(session_factory, client, settings)
                await notify_low_router_capacity(session_factory, bot, settings, snapshot)

            now = datetime.now(UTC)
            async with session_factory() as session:
                pending_ids = list(
                    await session.scalars(
                        select(RouterTokenPurchase.id)
                        .where(
                            RouterTokenPurchase.status.in_({"pending", "retry", "provisioning"}),
                            or_(
                                RouterTokenPurchase.next_retry_at.is_(None),
                                RouterTokenPurchase.next_retry_at <= now,
                            ),
                        )
                        .order_by(RouterTokenPurchase.id)
                        .limit(10)
                    )
                )
            for purchase_id in pending_ids:
                await fulfill_router_token_purchase(session_factory, client, cipher, purchase_id)

            async with session_factory() as session:
                notify_ids = list(
                    await session.scalars(
                        select(RouterTokenPurchase.id)
                        .where(
                            RouterTokenPurchase.status == "fulfilled",
                            RouterTokenPurchase.notified_at.is_(None),
                        )
                        .order_by(RouterTokenPurchase.id)
                        .limit(10)
                    )
                )
            for purchase_id in notify_ids:
                await notify_router_token_purchase(
                    session_factory,
                    bot,
                    cipher,
                    client.public_api_url,
                    purchase_id,
                    client.usage_page_url,
                )
        except Exception:
            logger.exception("9Router token worker failed")
        await asyncio.sleep(max(2, interval_seconds))


async def router_purchase_for_order(
    session: AsyncSession,
    user_id: int,
    order_id: int,
) -> RouterTokenPurchase | None:
    return await session.scalar(
        select(RouterTokenPurchase).where(
            RouterTokenPurchase.user_id == user_id,
            RouterTokenPurchase.order_id == order_id,
        )
    )
