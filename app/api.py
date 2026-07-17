import hmac
import json
import logging
from pathlib import Path

from aiogram import Bot
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from app.config import Settings
from app.dashboard import create_dashboard_router
from app.deposit_notifications import send_deposit_notification
from app.delivery import delivery_keyboard, delivery_text
from app.keyboards import main_menu
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
) -> FastAPI:
    app = FastAPI(title="Telegram SePay Shop", docs_url=None, redoc_url=None)
    if settings.dashboard_enabled:
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
            )
        )

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
        raw_body = await request.body()
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
        result = await process_sepay_payment(
            session_factory,
            payload,
            settings.payment_prefix,
            cipher,
            supplier_client,
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

        return {"success": True, "status": result.status}

    return app
