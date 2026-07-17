from aiogram.types import (
    BufferedInputFile,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from app.utils import format_vnd, safe_html


MAX_MESSAGE_PREVIEW = 10
MAX_COPY_BUTTONS = 8
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
    preview = secrets[:MAX_MESSAGE_PREVIEW]
    items = "\n".join(
        f"{index}. <tg-spoiler>{safe_html(secret)}</tg-spoiler>"
        for index, secret in enumerate(preview, start=1)
    )
    remaining = len(secrets) - len(preview)
    if remaining > 0:
        items += (
            f"\n… còn {remaining} tài khoản trong file TXT."
            if language == "vi"
            else f"\n… {remaining} more items are available in the TXT file."
        )

    if language == "en":
        title = "Payment and delivery successful" if paid_by_qr else "Purchase successful"
        return (
            f"✅ <b>{title}</b>\n\n"
            f"• Shop order: <code>{safe_html(shop_order_code)}</code>\n"
            f"• Product: <b>{safe_html(product_name)}</b>\n"
            f"• Quantity: <b>{len(secrets)}</b>\n"
            f"• Total: <b>{format_vnd(total_amount)}</b>\n\n"
            f"<b>Your accounts/codes</b>\n{items}\n\n"
            "Use the copy buttons or download the TXT file. Keep this information private."
        )

    title = "Thanh toán và giao hàng thành công" if paid_by_qr else "Mua hàng thành công"
    return (
        f"✅ <b>{title}</b>\n\n"
        f"• Mã đơn shop: <code>{safe_html(shop_order_code)}</code>\n"
        f"• Sản phẩm: <b>{safe_html(product_name)}</b>\n"
        f"• Số lượng: <b>{len(secrets)}</b>\n"
        f"• Tổng tiền: <b>{format_vnd(total_amount)}</b>\n\n"
        f"<b>Tài khoản/code của bạn</b>\n{items}\n\n"
        "Dùng nút sao chép hoặc tải file TXT. Không chia sẻ thông tin này cho người khác."
    )


def delivery_keyboard(
    *,
    primary_order_id: int,
    secrets: list[str],
    language: str,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    copy_label = "Sao chép" if language == "vi" else "Copy"
    for index, secret in enumerate(secrets[:MAX_COPY_BUTTONS], start=1):
        if len(secret) > MAX_COPY_TEXT_LENGTH:
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📋 {copy_label} #{index}",
                    copy_text=CopyTextButton(text=secret),
                )
            ]
        )

    combined = "\n".join(secrets)
    if len(secrets) > 1 and len(combined) <= MAX_COPY_TEXT_LENGTH:
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
    if language == "en":
        header = [
            "PURCHASED DIGITAL GOODS",
            f"Shop order: {shop_order_code}",
            f"Product: {product_name}",
            f"Quantity: {len(secrets)}",
            f"Total: {format_vnd(total_amount)}",
            "",
            "ACCOUNTS / CODES",
        ]
    else:
        header = [
            "THÔNG TIN SẢN PHẨM ĐÃ MUA",
            f"Mã đơn shop: {shop_order_code}",
            f"Sản phẩm: {product_name}",
            f"Số lượng: {len(secrets)}",
            f"Tổng tiền: {format_vnd(total_amount)}",
            "",
            "TÀI KHOẢN / CODE",
        ]
    body = [f"{index}. {secret}" for index, secret in enumerate(secrets, start=1)]
    content = "\n".join([*header, *body, "", "PHP Tool Shop"])
    safe_code = "".join(
        character for character in shop_order_code if character.isalnum() or character in "-_"
    )[:64] or "shop"
    return BufferedInputFile(
        content.encode("utf-8-sig"),
        filename=f"don-hang-{safe_code}.txt",
    )
