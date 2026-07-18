import hmac
import json
import logging
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from aiogram import Bot
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from app.config import Settings
from app.dashboard import create_dashboard_router
from app.dashboard_security import LoginRateLimiter
from app.deposit_notifications import send_deposit_notification
from app.delivery import delivery_keyboard, delivery_text
from app.keyboards import main_menu
from app.lehai_suppliers import LeHaiPremiumClient
from app.models import ApiRequestAudit
from app.public_api import client_ip, create_public_api_docs_router, create_public_api_router
from app.rate_limit import FixedWindowRateLimiter, RateLimitDecision, RateLimitRule
from app.services import process_sepay_payment
from app.suppliers import SumistoreClient
from app.utils import SecretCipher, format_vnd, verify_sepay_hmac


logger = logging.getLogger(__name__)


def safe_log_value(value: object, limit: int = 180) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ")[:limit]


def normalize_api_key(value: str | None) -> str:
    if not value:
        return ""
    parts = value.strip().split(maxsplit=1)
    if len(parts) == 2 and parts[0].lower() in {"apikey", "bearer"}:
        return parts[1]
    return value.strip()


def create_api(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    cipher: SecretCipher,
    supplier_client: SumistoreClient | None = None,
    deposit_notification_bot: Bot | None = None,
    api_redis: Redis | None = None,
    lehai_client: LeHaiPremiumClient | None = None,
) -> FastAPI:
    owned_api_redis = api_redis is None
    api_redis_client = api_redis or Redis.from_url(settings.redis_url, decode_responses=True)
    ingress_limiter = FixedWindowRateLimiter(api_redis_client, "http-limit")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            if owned_api_redis:
                await api_redis_client.aclose()

    app = FastAPI(
        title="Telegram SePay Shop",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    def rate_limited_response(decision: RateLimitDecision) -> JSONResponse:
        return JSONResponse(
            {
                "detail": {
                    "code": "RATE_LIMITED",
                    "message": "Too many requests",
                }
            },
            status_code=429,
            headers={
                "Retry-After": str(decision.retry_after),
                "Cache-Control": "no-store",
            },
        )

    @app.middleware("http")
    async def limit_public_ingress(request: Request, call_next):
        path = request.url.path
        remote_ip = client_ip(request) or "unknown"
        if request.method == "POST" and path == "/webhooks/sepay":
            per_ip = await ingress_limiter.hit(
                f"sepay:ip:{remote_ip}",
                (
                    RateLimitRule("burst", 20, 10),
                    RateLimitRule(
                        "minute",
                        settings.sepay_webhook_rate_limit_per_minute,
                        60,
                    ),
                ),
            )
            if not per_ip.allowed:
                return rate_limited_response(per_ip)
            global_limit = await ingress_limiter.hit(
                "sepay:global",
                (
                    RateLimitRule(
                        "minute",
                        settings.sepay_webhook_global_rate_limit_per_minute,
                        60,
                    ),
                ),
            )
            if not global_limit.allowed:
                return rate_limited_response(global_limit)
        elif path.startswith("/v1/") and path not in {"/v1/health", "/v1/docs"}:
            per_ip = await ingress_limiter.hit(
                f"api:ip:{remote_ip}",
                (
                    RateLimitRule("burst", 40, 10),
                    RateLimitRule(
                        "minute",
                        settings.public_api_ip_rate_limit_per_minute,
                        60,
                    ),
                ),
            )
            if not per_ip.allowed:
                return rate_limited_response(per_ip)
            global_limit = await ingress_limiter.hit(
                "api:global",
                (
                    RateLimitRule(
                        "minute",
                        settings.public_api_global_rate_limit_per_minute,
                        60,
                    ),
                ),
            )
            if not global_limit.allowed:
                return rate_limited_response(global_limit)
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    if settings.dashboard_enabled:
        login_rate_limiter = LoginRateLimiter()

        @app.middleware("http")
        async def limit_admin_login(request: Request, call_next):
            if request.method != "POST" or request.url.path != "/admin/login":
                return await call_next(request)
            key = client_ip(request) or "unknown"
            if login_rate_limiter.blocked(key):
                return HTMLResponse(
                    "Đăng nhập sai quá nhiều lần. Vui lòng thử lại sau 5 phút.",
                    status_code=429,
                    headers={"Retry-After": "300"},
                )
            response = await call_next(request)
            if response.status_code == 401:
                login_rate_limiter.record_failure(key)
            elif response.status_code in {302, 303}:
                login_rate_limiter.reset(key)
            return response

        app.add_middleware(
            SessionMiddleware,
            secret_key=settings.dashboard_session_secret.get_secret_value(),
            session_cookie="shop_admin_session",
            same_site="lax",
            https_only=True,
            max_age=8 * 60 * 60,
        )
        app.mount(
            "/admin-assets",
            StaticFiles(directory=Path(__file__).parent / "static"),
            name="admin-assets",
        )

        @app.middleware("http")
        async def cache_admin_assets(request: Request, call_next):
            response = await call_next(request)
            if request.url.path.startswith("/admin-assets/"):
                response.headers["Cache-Control"] = "public, max-age=86400, immutable"
            return response

        app.include_router(
            create_dashboard_router(
                settings,
                session_factory,
                cipher,
                supplier_client,
                lehai_client,
            )
        )

    if settings.shop_api_enabled:
        app.include_router(create_public_api_docs_router(settings))
        app.include_router(
            create_public_api_router(
                settings,
                session_factory,
                cipher,
                supplier_client,
                api_redis_client,
                lehai_client=lehai_client,
            )
        )

        @app.middleware("http")
        async def audit_shop_api(request: Request, call_next):
            if not request.url.path.startswith("/v1"):
                return await call_next(request)
            started = time.perf_counter()
            status_code = 500
            try:
                response = await call_next(request)
                status_code = response.status_code
                return response
            finally:
                with suppress(Exception):
                    async with session_factory() as session:
                        session.add(
                            ApiRequestAudit(
                                api_client_id=getattr(request.state, "api_client_id", None),
                                method=request.method,
                                path=request.url.path[:255],
                                status_code=status_code,
                                client_ip=client_ip(request)[:64],
                                duration_ms=int((time.perf_counter() - started) * 1000),
                            )
                        )
                        await session.commit()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/sepay")
    async def sepay_webhook(
        request: Request,
        authorization: str | None = Header(default=None),
        x_sepay_api_key: str | None = Header(default=None),
        x_sepay_signature: str | None = Header(default=None),
        x_sepay_timestamp: str | None = Header(default=None),
    ) -> dict[str, object]:
        if not settings.sepay_enabled:
            raise HTTPException(status_code=503, detail="SePay integration is disabled")
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > 64 * 1024:
            raise HTTPException(status_code=413, detail="Webhook payload is too large")
        raw_body = await request.body()
        if len(raw_body) > 64 * 1024:
            raise HTTPException(status_code=413, detail="Webhook payload is too large")
        if settings.sepay_auth_mode == "hmac":
            if not verify_sepay_hmac(
                raw_body,
                x_sepay_signature,
                x_sepay_timestamp,
                settings.sepay_webhook_secret.get_secret_value(),
            ):
                raise HTTPException(status_code=401, detail="Invalid webhook signature")
        else:
            supplied_key = normalize_api_key(authorization or x_sepay_api_key)
            expected_key = settings.sepay_api_key.get_secret_value()
            if not supplied_key or not hmac.compare_digest(supplied_key, expected_key):
                raise HTTPException(status_code=401, detail="Invalid webhook credentials")

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid payload")
        fulfillment_message: tuple[int, int] | None = None

        async def show_fulfillment_started(user_id: int, language: str) -> None:
            nonlocal fulfillment_message
            try:
                message = await bot.send_message(
                    user_id,
                    (
                        "✅ <b>Thanh toán thành công</b>\n"
                        "⏳ Đang lấy hàng, bạn vui lòng chờ trong giây lát..."
                        if language == "vi"
                        else "✅ <b>Payment successful</b>\n"
                        "⏳ Getting your product, please wait a moment..."
                    ),
                )
            except Exception:
                logger.exception("Could not send fulfillment status to user %s", user_id)
                return
            message_id = getattr(message, "message_id", None)
            if message_id is not None:
                fulfillment_message = (user_id, int(message_id))

        try:
            result = await process_sepay_payment(
                session_factory,
                payload,
                settings.payment_prefix,
                cipher,
                supplier_client,
                settings.referral_commission_percent,
                show_fulfillment_started,
                lehai_client=lehai_client,
            )
        finally:
            if fulfillment_message is not None:
                chat_id, message_id = fulfillment_message
                try:
                    await bot.delete_message(chat_id, message_id)
                except Exception:
                    logger.warning(
                        "Could not delete fulfillment status message user=%s message=%s",
                        chat_id,
                        message_id,
                    )
        logger.info("SePay webhook processed: status=%s", result.status)
        if result.status == "deposit_not_found":
            logger.warning(
                "Unmatched SePay payment: id=%s amount=%s code=%r content=%r description=%r",
                safe_log_value(payload.get("id") or payload.get("referenceCode")),
                safe_log_value(payload.get("transferAmount") or payload.get("amount")),
                safe_log_value(payload.get("code")),
                safe_log_value(payload.get("content")),
                safe_log_value(payload.get("description")),
            )

        if (
            deposit_notification_bot is not None
            and result.user_id is not None
            and result.status
            in {
                "credited",
                "direct_purchase_completed",
                "direct_purchase_fallback",
                "expired_payment",
                "amount_mismatch",
                "already_paid_payment",
                "failed_request_payment",
            }
        ):
            await send_deposit_notification(
                deposit_notification_bot,
                settings.admin_ids,
                result,
            )

        if result.status == "credited" and result.user_id is not None:
            try:
                balance_line = (
                    f"\nSố dư mới: <b>{format_vnd(result.balance)}</b>"
                    if result.balance is not None
                    else ""
                )
                await bot.send_message(
                    result.user_id,
                    "✅ <b>Nạp tiền thành công</b>\n"
                    f"Số tiền: <b>{format_vnd(result.amount)}</b>\n"
                    f"{balance_line}\n\n"
                    "Số dư đã được cập nhật, bạn có thể mua hàng ngay.",
                    reply_markup=main_menu(result.language),
                )
            except Exception:
                logger.exception("Could not notify user %s about deposit", result.user_id)

        if result.status == "direct_purchase_completed" and result.user_id is not None:
            try:
                product_name = (
                    result.product_name_en if result.language == "en" else result.product_name_vi
                ) or "Digital product"
                secret_values = [cipher.decrypt(value) for value in result.encrypted_secrets]
                order_ids = list(result.order_ids)
                await bot.send_message(
                    result.user_id,
                    delivery_text(
                        shop_order_code=result.shop_order_code or f"O{min(order_ids)}",
                        product_name=product_name,
                        secrets=secret_values,
                        total_amount=result.amount,
                        language=result.language,
                        paid_by_qr=True,
                    ),
                    reply_markup=delivery_keyboard(
                        primary_order_id=min(order_ids),
                        secrets=secret_values,
                        language=result.language,
                    ),
                )
            except Exception:
                logger.exception(
                    "Could not deliver direct purchase to user %s",
                    result.user_id,
                )

        if result.status == "direct_purchase_fallback" and result.user_id is not None:
            try:
                new_balance = (
                    f"\nSố dư mới: <b>{format_vnd(result.balance)}</b>"
                    if result.balance is not None and result.language == "vi"
                    else f"\nNew balance: <b>{format_vnd(result.balance)}</b>"
                    if result.balance is not None
                    else ""
                )
                text = (
                    "⚠️ Thanh toán đã được ghi nhận nhưng sản phẩm không thể giao tự động.\n"
                    f"Số tiền <b>{format_vnd(result.amount)}</b> đã được cộng vào số dư của bạn."
                    f"{new_balance}"
                    if result.language == "vi"
                    else "⚠️ Payment was recorded, but the product could not be delivered automatically.\n"
                    f"<b>{format_vnd(result.amount)}</b> was added to your balance."
                    f"{new_balance}"
                )
                await bot.send_message(
                    result.user_id,
                    text,
                    reply_markup=main_menu(result.language),
                )
            except Exception:
                logger.exception(
                    "Could not notify user %s about direct payment fallback",
                    result.user_id,
                )

        rejected_payment_messages = {
            "expired_payment": (
                "⚠️ Giao dịch đến sau khi QR đã hết hạn 5 phút nên hệ thống không cộng tiền. "
                "Vui lòng liên hệ hỗ trợ để được kiểm tra."
                if result.language == "vi"
                else "⚠️ The transfer arrived after the 5-minute QR expiry and was not credited. "
                "Please contact support for review."
            ),
            "amount_mismatch": (
                "⚠️ Số tiền chuyển không khớp chính xác với QR nên hệ thống không cộng tiền. "
                "Vui lòng liên hệ hỗ trợ để được kiểm tra."
                if result.language == "vi"
                else "⚠️ The transfer amount did not exactly match the QR and was not credited. "
                "Please contact support for review."
            ),
            "already_paid_payment": (
                "⚠️ Mã QR này đã được thanh toán trước đó nên giao dịch mới không được cộng lần hai. "
                "Vui lòng liên hệ hỗ trợ nếu bạn đã chuyển tiền."
                if result.language == "vi"
                else "⚠️ This QR was already paid, so the new transfer was not credited again. "
                "Please contact support if you sent money."
            ),
            "failed_request_payment": (
                "⚠️ Yêu cầu thanh toán này đã thất bại nên giao dịch không được tự động cộng tiền. "
                "Vui lòng liên hệ hỗ trợ để được kiểm tra."
                if result.language == "vi"
                else "⚠️ This payment request had failed, so the transfer was not credited. "
                "Please contact support for review."
            ),
        }
        rejected_message = rejected_payment_messages.get(result.status)
        if rejected_message is not None and result.user_id is not None:
            try:
                await bot.send_message(
                    result.user_id,
                    rejected_message,
                    reply_markup=main_menu(result.language),
                )
            except Exception:
                logger.exception(
                    "Could not notify user %s about rejected payment %s",
                    result.user_id,
                    result.status,
                )

        return {"success": True, "status": result.status}

    return app
