import json

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


def codex_config_content(api_url: str) -> str:
    normalized_api_url = api_url.rstrip("/").replace("\\", "\\\\").replace('"', '\\"')
    return (
        "# 9Router Configuration for Codex CLI\n"
        'model = "cx/gpt-5.6-sol"\n'
        'model_provider = "9router"\n\n'
        "[model_providers.9router]\n"
        'name = "9Router"\n'
        f'base_url = "{normalized_api_url}"\n'
        'wire_api = "responses"\n\n'
        "[agents.subagent]\n"
        'model = "cx/gpt-5.6-sol"'
    )


def codex_auth_content(api_key: str) -> str:
    return json.dumps(
        {"auth_mode": "apikey", "OPENAI_API_KEY": api_key},
        ensure_ascii=True,
        indent=2,
    )


def codex_setup_text(
    *,
    filename: str,
    content: str,
    code_language: str,
    step: int,
    language: str,
) -> str:
    escaped_filename = safe_html(filename)
    escaped_content = safe_html(content)
    if language == "en":
        folder_help = (
            "On Windows, open File Explorer and paste "
            "<code>%USERPROFILE%\\.codex</code> into the address bar "
            "(for example <code>C:\\Users\\your-name\\.codex</code>). "
            "Create the <code>.codex</code> folder if it does not exist.\n\n"
            if step == 1
            else "Open the same <code>.codex</code> folder from step 1.\n\n"
        )
        return (
            f"⚙️ <b>Codex setup {step}/2: <code>{escaped_filename}</code></b>\n\n"
            f"{folder_help}"
            "Create or open this file, then paste the complete content below:\n\n"
            f'<pre><code class="language-{code_language}">{escaped_content}</code></pre>\n\n'
            "Use the button below to copy the whole block."
        )
    folder_help = (
        "Trên Windows, mở File Explorer rồi dán "
        "<code>%USERPROFILE%\\.codex</code> vào thanh địa chỉ "
        "(ví dụ <code>C:\\Users\\tên-user\\.codex</code>). "
        "Nếu chưa có thì tạo thư mục <code>.codex</code>.\n\n"
        if step == 1
        else "Mở lại thư mục <code>.codex</code> ở bước 1.\n\n"
    )
    return (
        f"⚙️ <b>Cài Codex bước {step}/2: <code>{escaped_filename}</code></b>\n\n"
        f"{folder_help}"
        "Tạo hoặc mở file này rồi dán nguyên nội dung bên dưới:\n\n"
        f'<pre><code class="language-{code_language}">{escaped_content}</code></pre>\n\n'
        "Dùng nút bên dưới để sao chép toàn bộ khối code."
    )


def codex_setup_keyboard(
    *,
    filename: str,
    content: str,
    language: str,
) -> InlineKeyboardMarkup:
    label = f"📋 Sao chép {filename}" if language == "vi" else f"📋 Copy {filename}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, copy_text=CopyTextButton(text=content))]
        ]
    )


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


def router_token_delivery_text(
    *,
    shop_order_code: str,
    product_name: str,
    api_url: str,
    api_key: str,
    token_quota: int,
    paid_amount: int,
    language: str,
) -> str:
    quota = f"{token_quota:,}".replace(",", ".")
    if language == "en":
        return (
            "✅ <b>GPT token key is ready</b>\n\n"
            f"• Shop order: <code>{safe_html(shop_order_code)}</code>\n"
            f"• Product: <b>{safe_html(product_name)}</b>\n"
            f"• Paid: <b>{format_vnd(paid_amount)}</b>\n"
            f"• Combined quota: <b>{quota} tokens</b>\n"
            f"• API URL: <code>{safe_html(api_url)}</code>\n"
            f"• API key: <tg-spoiler><code>{safe_html(api_key)}</code></tg-spoiler>\n\n"
            "The key stops automatically when its token balance reaches zero."
        )
    return (
        "✅ <b>Đã cấp key GPT token</b>\n\n"
        f"• Mã đơn shop: <code>{safe_html(shop_order_code)}</code>\n"
        f"• Sản phẩm: <b>{safe_html(product_name)}</b>\n"
        f"• Đã thanh toán: <b>{format_vnd(paid_amount)}</b>\n"
        f"• Tổng quota: <b>{quota} token</b>\n"
        f"• API URL: <code>{safe_html(api_url)}</code>\n"
        f"• API key: <tg-spoiler><code>{safe_html(api_key)}</code></tg-spoiler>\n\n"
        "Key tự ngắt khi số token còn lại về 0."
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
