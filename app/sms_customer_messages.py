"""Customer-facing SMS rental messages.

Keep upstream service names, URLs, keys, response bodies, and error codes out of
Telegram messages. Technical details remain available to the admin reconciliation
and server logs.
"""

from html import escape

from app.utils import format_vnd, safe_customer_html


def storefront_text(
    language: str,
    *,
    connected: bool,
    sale_price: int,
    effective_stock: int,
) -> str:
    if language == "en":
        status = (
            f"• Available now: <b>{effective_stock}</b>"
            if connected
            else "• Status: <b>temporarily unavailable</b>"
        )
        return (
            "📲 <b>Rent a ChatGPT SMS number</b>\n\n"
            "• Country: <b>Cambodia</b>\n"
            f"• Price: <b>{format_vnd(sale_price)}</b>\n"
            f"{status}\n\n"
            "Wallet balance only. Direct QR payment is not available for SMS rentals.\n"
            "If no OTP arrives, you can rent another number after 60 seconds.\n"
            "Numbers that do not receive an OTP are refunded to your wallet.\n\n"
            "An OTP may arrive late. If you rent a new number and the old number later "
            "receives a code, both rentals are charged."
        )
    status = (
        f"• Có thể thuê ngay: <b>{effective_stock}</b> số"
        if connected
        else "• Trạng thái: <b>đang tạm gián đoạn</b>"
    )
    return (
        "📲 <b>Thuê số nhận SMS ChatGPT</b>\n\n"
        "• Quốc gia: <b>Cambodia</b>\n"
        f"• Giá thuê: <b>{format_vnd(sale_price)}</b>\n"
        f"{status}\n\n"
        "Chỉ thanh toán bằng số dư ví, không có QR thanh toán trực tiếp.\n\n"
        "Nếu không có OTP có thể thuê số khác sau 60 giây.\n"
        "Các số thuê không nhận được OTP sẽ được hoàn tiền về ví.\n\n"
        "OTP có thể về chậm. Nếu bạn thuê số mới và số cũ sau đó vẫn nhận được mã, "
        "cả hai lượt thuê đều được tính phí."
    )


def rental_failure_text(
    language: str,
    *,
    message: str,
    status: str,
    sale_amount: int,
    balance: int,
    retry_after: int,
) -> str:
    """Render a safe failure message without interpolating internal error data."""
    if language == "en":
        messages = {
            "disabled": "SMS rental is not available yet.",
            "out_of_stock": (
                "ChatGPT Cambodia numbers are currently unavailable. Your wallet was not "
                "charged or has already been refunded."
            ),
            "insufficient": (
                f"Your wallet needs {format_vnd(sale_amount)}, but currently has "
                f"{format_vnd(balance)}. Please deposit into your wallet first."
            ),
            "cooldown": (
                f"Please wait another {retry_after} seconds before renting another number."
            ),
            "blocked": "Your account is blocked. Please contact support.",
            "invalid_key": "SMS rental is temporarily unavailable. Please try again later.",
            "provider_unavailable": (
                "SMS rental is temporarily unavailable. Your full rental amount has been "
                "refunded. Please try again after 60 seconds."
            ),
            "provider_result_unknown": (
                f"The rental result is unclear. {format_vnd(sale_amount)} is temporarily "
                "held for review; it has not been marked as successful or refunded yet."
            ),
            "provider_error_refunded": (
                "The rental could not be confirmed. Your full rental amount has been refunded. "
                "Please try again after 60 seconds."
            ),
        }
        default = (
            "The rental could not be completed. Your wallet has been refunded."
            if status == "refunded"
            else "The rental could not be completed and your wallet was not charged."
        )
    else:
        messages = {
            "disabled": "Chức năng thuê số hiện chưa sẵn sàng.",
            "out_of_stock": (
                "Số ChatGPT Cambodia hiện chưa có. Ví không bị trừ hoặc tiền giữ đã được hoàn lại."
            ),
            "insufficient": (
                f"Ví cần {format_vnd(sale_amount)} nhưng hiện có {format_vnd(balance)}. "
                "Hãy nạp vào ví trước."
            ),
            "cooldown": (
                f"Bạn cần chờ thêm {retry_after} giây mới được thuê số tiếp theo."
            ),
            "blocked": "Tài khoản đang bị khóa. Hãy liên hệ hỗ trợ.",
            "invalid_key": "Dịch vụ thuê số đang tạm gián đoạn. Vui lòng thử lại sau.",
            "provider_unavailable": (
                "Dịch vụ thuê số đang tạm gián đoạn. Toàn bộ tiền thuê đã được hoàn vào ví. "
                "Bạn thử thuê lại sau 60 giây."
            ),
            "provider_result_unknown": (
                f"Kết quả thuê số chưa xác định. Khoản {format_vnd(sale_amount)} đang được "
                "tạm giữ để đối soát, chưa tính là thuê thành công và chưa tự động hoàn. "
                "Admin đã được cảnh báo để kiểm tra."
            ),
            "provider_error_refunded": (
                "Lượt thuê chưa được xác nhận nên toàn bộ tiền đã được hoàn vào ví. "
                "Bạn thử thuê lại sau 60 giây."
            ),
        }
        default = (
            "Không thể thuê số. Nếu ví đã bị trừ thì hệ thống đã tự động hoàn lại."
            if status == "refunded"
            else "Không thể thuê số và ví không bị trừ tiền."
        )
    return f"⚠️ {messages.get(message, default)}"


def poll_notification_text(item) -> str:
    """Render OTP/refund/review notifications using only shop-level wording."""
    if item.status == "success":
        if item.language == "en":
            return (
                "✅ <b>OTP received</b>\n\n"
                f"• Order: <code>{escape(item.shop_order_code)}</code>\n"
                f"• Number: <code>{escape(item.phone_number)}</code>\n"
                f"• OTP: <code>{escape(item.otp_code or '—')}</code>\n"
                f"• Message: {safe_customer_html(item.otp_content or '—')}\n\n"
                "You can rent another number once 60 seconds have passed from this rental."
            )
        return (
            "✅ <b>Đã nhận được OTP</b>\n\n"
            f"• Mã đơn: <code>{escape(item.shop_order_code)}</code>\n"
            f"• Số điện thoại: <code>{escape(item.phone_number)}</code>\n"
            f"• Mã OTP: <code>{escape(item.otp_code or '—')}</code>\n"
            f"• Nội dung: {safe_customer_html(item.otp_content or '—')}\n\n"
            "Bạn có thể thuê số tiếp theo sau khi đủ 60 giây tính từ lượt thuê này."
        )
    if item.status == "refunded":
        if item.language == "en":
            if item.failure_reason == "provider_request_not_confirmed":
                reason = "The rental could not be confirmed, so the full amount was refunded."
            else:
                reason = "This rented number did not receive an OTP, so the rental was refunded in full."
            return (
                "↩️ <b>SMS rental was refunded</b>\n\n"
                f"• Order: <code>{escape(item.shop_order_code)}</code>\n"
                + (
                    f"• Rented number: <code>{escape(item.phone_number or '—')}</code>\n"
                    if item.failure_reason != "provider_request_not_confirmed"
                    else ""
                )
                + f"• Refunded: <b>{format_vnd(item.sale_amount)}</b>\n"
                f"• Wallet balance: <b>{format_vnd(item.balance)}</b>\n\n{reason}"
            )
        if item.failure_reason == "provider_request_not_confirmed":
            reason = "Lượt thuê chưa được xác nhận nên toàn bộ tiền thuê đã được hoàn lại."
            number_line = ""
        else:
            number = escape(item.phone_number or "—")
            reason = f"Số {number} không nhận được mã OTP nên tiền thuê đã được hoàn lại đầy đủ."
            number_line = f"• Số thuê: <code>{number}</code>\n"
        return (
            "↩️ <b>Đã hoàn tiền thuê số</b>\n\n"
            f"• Mã đơn: <code>{escape(item.shop_order_code)}</code>\n"
            f"{number_line}"
            f"• Đã hoàn ví: <b>{format_vnd(item.sale_amount)}</b>\n"
            f"• Số dư hiện tại: <b>{format_vnd(item.balance)}</b>\n\n{reason}"
        )
    if item.language == "en":
        return (
            "⚠️ <b>SMS rental needs review</b>\n\n"
            f"• Order: <code>{escape(item.shop_order_code)}</code>\n"
            f"• Temporarily held: <b>{format_vnd(item.sale_amount)}</b>\n"
            f"• Wallet balance: <b>{format_vnd(item.balance)}</b>\n\n"
            "The rental result is not confirmed. It is not marked successful and has not been "
            "automatically refunded. Admin has been notified for review."
        )
    return (
        "⚠️ <b>Đơn thuê số cần đối soát</b>\n\n"
        f"• Mã đơn: <code>{escape(item.shop_order_code)}</code>\n"
        f"• Đang tạm giữ: <b>{format_vnd(item.sale_amount)}</b>\n"
        f"• Số dư ví: <b>{format_vnd(item.balance)}</b>\n\n"
        "Kết quả thuê số chưa xác định nên shop chưa tính là thuê thành công và không tự động "
        "hoàn nhầm. Admin đã được cảnh báo để kiểm tra."
    )
