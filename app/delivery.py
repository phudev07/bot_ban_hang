from aiogram.types import (
    BufferedInputFile,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from app.custom_emoji import product_brand_emoji
from app.utils import format_vnd, safe_html, sanitize_customer_text


MAX_MESSAGE_PREVIEW = 10
MAX_COPY_TEXT_LENGTH = 256


def delivery_text(
    *,
    shop_order_code: str,
    product_name: str,
    secrets: list[str],
    total_amount: int,
    language: str,
    paid_by_qr: bool = False,
) -> str:
    product_name = sanitize_customer_text(product_name)
    is_codex_key = "codex" in product_name.casefold()
    brand_emoji = product_brand_emoji(product_name)
    preview = secrets[:MAX_MESSAGE_PREVIEW]
    items = "\n".join(safe_html(secret) for secret in preview)
    account_block = f"<pre>{items}</pre>"
    remaining = len(secrets) - len(preview)
    if remaining > 0:
        account_block += (
            f"\n… còn {remaining} tài khoản trong file TXT."
            if language == "vi"
            else f"\n… {remaining} more items are available in the TXT file."
        )

    if language == "en":
        title = "Payment and delivery successful" if paid_by_qr else "Purchase successful"
        delivered_label = "Your activation key" if is_codex_key else "Your accounts/codes"
        return (
            f"✅ <b>{title}</b>\n\n"
            f"🧾 Shop order: <code>{safe_html(shop_order_code)}</code>\n"
            f"📦 Product: {brand_emoji} <b>{safe_html(product_name)}</b>\n"
            f"🧮 Quantity: <b>{len(secrets)}</b>\n"
            f"💰 Total: <b>{format_vnd(total_amount)}</b>\n\n"
            f"📋 <b>{delivered_label}:</b>\n{account_block}\n\n"
            "Use the copy-all button or download the TXT file. Keep this information private."
        )

    title = "Thanh toán và giao hàng thành công" if paid_by_qr else "Mua hàng thành công"
    delivered_label = "Key kích hoạt của bạn" if is_codex_key else "Tài khoản/code của bạn"
    return (
        f"✅ <b>{title}</b>\n\n"
        f"🧾 Mã đơn shop: <code>{safe_html(shop_order_code)}</code>\n"
        f"📦 Sản phẩm: {brand_emoji} <b>{safe_html(product_name)}</b>\n"
        f"🧮 Số lượng: <b>{len(secrets)}</b>\n"
        f"💰 Tổng tiền: <b>{format_vnd(total_amount)}</b>\n\n"
        f"📋 <b>{delivered_label}:</b>\n{account_block}\n\n"
        "Dùng nút sao chép hoặc tải file TXT. Không chia sẻ thông tin này cho người khác."
    )


def delivery_keyboard(
    *,
    primary_order_id: int,
    secrets: list[str],
    language: str,
    guide_url: str | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    combined = "\n".join(secrets)
    if combined and len(combined) <= MAX_COPY_TEXT_LENGTH:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📋 Sao chép tất cả" if language == "vi" else "📋 Copy all",
                    copy_text=CopyTextButton(text=combined),
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="⬇️ Tải file tài khoản .txt"
                if language == "vi"
                else "⬇️ Download accounts .txt",
                callback_data=f"ordertxt:{primary_order_id}",
            )
        ]
    )
    if guide_url:
        rows.append(
            [
                InlineKeyboardButton(
                    text=(
                        "📘 Hướng dẫn kích hoạt API Codex"
                        if language == "vi"
                        else "📘 Codex API setup guide"
                    ),
                    url=guide_url,
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="📦 Đơn đã mua" if language == "vi" else "📦 Purchased orders",
                callback_data="menu:orders",
            ),
            InlineKeyboardButton(
                text="🏠 Menu",
                callback_data="back:menu",
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def delivery_file(
    *,
    shop_order_code: str,
    product_name: str,
    secrets: list[str],
    total_amount: int,
    language: str,
) -> BufferedInputFile:
    product_name = sanitize_customer_text(product_name)
    is_codex_key = "codex" in product_name.casefold()
    if language == "en":
        header = [
            "PURCHASED DIGITAL GOODS",
            f"Shop order: {shop_order_code}",
            f"Product: {product_name}",
            f"Quantity: {len(secrets)}",
            f"Total: {format_vnd(total_amount)}",
            "",
            "ACTIVATION KEY" if is_codex_key else "ACCOUNTS / CODES",
        ]
    else:
        header = [
            "THÔNG TIN SẢN PHẨM ĐÃ MUA",
            f"Mã đơn shop: {shop_order_code}",
            f"Sản phẩm: {product_name}",
            f"Số lượng: {len(secrets)}",
            f"Tổng tiền: {format_vnd(total_amount)}",
            "",
            "KEY KÍCH HOẠT" if is_codex_key else "TÀI KHOẢN / CODE",
        ]
    body = list(secrets)
    content = "\n".join([*header, *body, "", "PHP Tool Shop"])
    safe_code = "".join(
        character for character in shop_order_code if character.isalnum() or character in "-_"
    )[:64] or "shop"
    return BufferedInputFile(
        content.encode("utf-8-sig"),
        filename=f"don-hang-{safe_code}.txt",
    )
