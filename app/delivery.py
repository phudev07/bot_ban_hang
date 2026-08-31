import json

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
MAX_DELIVERY_TEXT_LENGTH = 3_500
GPT_FREE_PRODUCT_ID = 28


def _codex_json_payload(secret: str) -> tuple[str | None, object | None]:
    """Extract the API key and parsed payload from a Cockpit Codex JSON item."""
    raw = str(secret or "").strip()
    if not raw or raw[0] not in "[{":
        return None, None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None, None
    records = payload if isinstance(payload, list) else [payload]
    for record in records:
        if isinstance(record, dict):
            key = record.get("OPENAI_API_KEY")
            if isinstance(key, str) and key.strip():
                return key.strip(), payload
    return None, None


def _codex_display_secret(secret: str) -> str:
    key, _payload = _codex_json_payload(secret)
    return key or secret


def _is_codex_json_delivery(product_name: str, secrets: list[str]) -> bool:
    return "codex" in product_name.casefold() and any(
        _codex_json_payload(secret)[0] for secret in secrets
    )


def _gpt_free_account_line(secret: str) -> str:
    """Return the login/2FA portion while preserving the complete raw payload separately."""
    parts = secret.split("|", 3)
    return "|".join(parts[:3]) if len(parts) >= 4 else secret


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
    is_gpt_free = "gpt free" in product_name.casefold()
    is_codex_key = "codex" in product_name.casefold() and not is_gpt_free
    is_codex_json = _is_codex_json_delivery(product_name, secrets)
    brand_emoji = product_brand_emoji(product_name)
    if is_gpt_free:
        display_secrets = [_gpt_free_account_line(secret) for secret in secrets]
    elif is_codex_json:
        display_secrets = [_codex_display_secret(secret) for secret in secrets]
    else:
        display_secrets = secrets
    preview: list[str] = []
    preview_length = 0
    for secret in display_secrets[:MAX_MESSAGE_PREVIEW]:
        rendered = safe_html(secret)
        extra_length = len(rendered) + (1 if preview else 0)
        if preview_length + extra_length > MAX_DELIVERY_TEXT_LENGTH:
            break
        preview.append(rendered)
        preview_length += extra_length
    items = "\n".join(preview)
    account_block = f"<pre>{items}</pre>"
    remaining = len(display_secrets) - len(preview)
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
    include_file_button: bool = True,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    copy_secrets = [
        _codex_display_secret(secret) if _codex_json_payload(secret)[0] else secret
        for secret in secrets
    ]
    combined = "\n".join(copy_secrets)
    if combined and len(combined) <= MAX_COPY_TEXT_LENGTH:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📋 Sao chép tất cả" if language == "vi" else "📋 Copy all",
                    copy_text=CopyTextButton(text=combined),
                )
            ]
        )

    if include_file_button:
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
    product_id: int | None = None,
    file_extension: str = "txt",
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
    extension = "".join(character for character in file_extension if character.isalnum()) or "txt"
    filename_prefix = "don-hang" if language == "vi" else "order"
    return BufferedInputFile(
        content.encode("utf-8-sig"),
        filename=f"{filename_prefix}-{safe_code}.{extension}",
    )


def delivery_files(
    *,
    shop_order_code: str,
    product_name: str,
    secrets: list[str],
    total_amount: int,
    language: str,
    product_id: int | None = None,
) -> list[BufferedInputFile]:
    """Build customer files, including GPT Free and Cockpit Codex formats."""
    if product_id != GPT_FREE_PRODUCT_ID:
        if _is_codex_json_delivery(product_name, secrets):
            payloads: list[object] = []
            for secret in secrets:
                _key, payload = _codex_json_payload(secret)
                if isinstance(payload, list):
                    payloads.extend(payload)
                elif payload is not None:
                    payloads.append(payload)
            if payloads:
                safe_code = "".join(
                    character
                    for character in shop_order_code
                    if character.isalnum() or character in "-_"
                )[:64] or "shop"
                filename_prefix = "don-hang" if language == "vi" else "order"
                content = json.dumps(payloads, ensure_ascii=False, indent=2).encode("utf-8")
                return [
                    BufferedInputFile(
                        content,
                        filename=f"{filename_prefix}-{safe_code}-full.json",
                    )
                ]
        return [
            delivery_file(
                shop_order_code=shop_order_code,
                product_name=product_name,
                secrets=secrets,
                total_amount=total_amount,
                language=language,
                product_id=product_id,
            )
        ]

    accounts = [_gpt_free_account_line(secret) for secret in secrets]
    account_file = delivery_file(
        shop_order_code=f"{shop_order_code}-acc",
        product_name=("GPT Free accounts" if language == "en" else "Tài khoản GPT Free"),
        secrets=accounts,
        total_amount=total_amount,
        language=language,
        product_id=product_id,
    )
    full_file = delivery_file(
        shop_order_code=f"{shop_order_code}-full",
        product_name=("GPT Free full data" if language == "en" else "GPT Free đầy đủ dữ liệu"),
        secrets=secrets,
        total_amount=total_amount,
        language=language,
        product_id=product_id,
        file_extension="json",
    )
    return [account_file, full_file]
