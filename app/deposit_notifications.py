import logging
from datetime import UTC
from zoneinfo import ZoneInfo

from aiogram import Bot

from app.services import PaymentResult
from app.utils import format_vnd, safe_html


logger = logging.getLogger(__name__)
LOCAL_TIMEZONE = ZoneInfo("Asia/Bangkok")


def deposit_notification_text(result: PaymentResult) -> str:
    username = f"@{safe_html(result.username)}" if result.username else "Khách Telegram"
    paid_at = result.paid_at
    if paid_at is None:
        paid_time = "—"
    else:
        if paid_at.tzinfo is None:
            paid_at = paid_at.replace(tzinfo=UTC)
        paid_time = paid_at.astimezone(LOCAL_TIMEZONE).strftime("%d/%m/%Y %H:%M:%S")
    base = (
        "💰 <b>Nạp tiền shop bot</b>\n"
        f"{username} đã nạp <b>{format_vnd(result.amount)}</b>\n"
        f"Mã nạp: <code>{safe_html(result.deposit_code or '—')}</code>\n"
        f"Thời gian: <b>{paid_time}</b>"
    )
    if result.status in {"credited", "direct_purchase_completed", "direct_purchase_fallback"}:
        return base
    status_labels = {
        "expired_payment": "Đến sau thời hạn 5 phút - không cộng tiền",
        "amount_mismatch": "Sai số tiền - không cộng tiền",
        "already_paid_payment": "QR đã được dùng - không cộng tiền lần hai",
        "failed_request_payment": "Yêu cầu đã thất bại - không cộng tiền",
    }
    return (
        "🚨 <b>Giao dịch cần kiểm tra</b>\n"
        f"{username} chuyển <b>{format_vnd(result.amount)}</b>\n"
        f"Mã nạp: <code>{safe_html(result.deposit_code or '—')}</code>\n"
        f"Kết quả: <b>{status_labels.get(result.status, safe_html(result.status))}</b>\n"
        f"Thời gian: <b>{paid_time}</b>"
    )


async def send_deposit_notification(
    bot: Bot,
    chat_ids: tuple[int, ...],
    result: PaymentResult,
) -> None:
    text = deposit_notification_text(result)
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id, text)
        except Exception:
            logger.exception("Could not send deposit notification to admin %s", chat_id)
