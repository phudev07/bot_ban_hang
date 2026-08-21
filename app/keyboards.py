from dataclasses import dataclass
from urllib.parse import quote

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.i18n import tr
from app.models import Category, Order, Preorder, Product
from app.utils import format_usd_from_vnd, format_vnd, sanitize_customer_text


@dataclass(frozen=True)
class SmsRentalSourceButton:
    key: str
    country_vi: str
    country_en: str
    price: int
    stock: int
    connected: bool


def quick_access_keyboard(language: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=tr(language, "menu")),
                KeyboardButton(text=tr(language, "quick")),
                KeyboardButton(text=tr(language, "deposit")),
            ]
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=(
            "Chọn thao tác nhanh…" if language == "vi" else "Choose a quick action…"
        ),
    )


def main_menu(
    language: str,
    *,
    sms_enabled: bool = False,
    codex_enabled: bool = False,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=tr(language, "quick"), callback_data="menu:quick"),
        InlineKeyboardButton(text=tr(language, "deposit"), callback_data="menu:deposit"),
    )
    builder.row(
        InlineKeyboardButton(text=tr(language, "codes"), callback_data="menu:codes"),
        InlineKeyboardButton(text=tr(language, "products"), callback_data="menu:products"),
    )
    if sms_enabled:
        builder.row(InlineKeyboardButton(text=tr(language, "sms"), callback_data="menu:sms"))
    if codex_enabled:
        builder.row(
            InlineKeyboardButton(
                text=tr(language, "codex_api"), callback_data="menu:codex-api"
            )
        )
    builder.row(
        InlineKeyboardButton(text=tr(language, "preorder"), callback_data="menu:preorders")
    )
    builder.row(
        InlineKeyboardButton(text=tr(language, "orders"), callback_data="menu:orders"),
        InlineKeyboardButton(text=tr(language, "profile"), callback_data="menu:profile"),
    )
    builder.row(
        InlineKeyboardButton(
            text=tr(language, "warehouse_api"), callback_data="menu:warehouse-api"
        ),
        InlineKeyboardButton(text=tr(language, "referral"), callback_data="menu:referral"),
    )
    builder.row(
        InlineKeyboardButton(text=tr(language, "support"), callback_data="menu:support"),
        InlineKeyboardButton(text=tr(language, "clear"), callback_data="menu:clear"),
    )
    builder.row(InlineKeyboardButton(text=tr(language, "language"), callback_data="menu:language"))
    return builder.as_markup()


def preorder_products_menu(
    products: list[Product],
    language: str,
    usd_to_vnd: int | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for product in products:
        name = sanitize_customer_text(
            product.name_en if language == "en" else product.name_vi
        )
        unit_price = (max(0, int(product.price)) * 105 + 99) // 100
        rows.append(
            [
                InlineKeyboardButton(
                    text=(
                        f"📦 {name} · {format_vnd(unit_price)}/1"
                        + (f" ({format_usd_from_vnd(unit_price, usd_to_vnd)})" if usd_to_vnd else "")
                    ),
                    callback_data=f"preorder:product:{product.id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🧾 Đơn đặt trước của tôi"
                if language == "vi"
                else "🧾 My preorders",
                callback_data="preorder:history",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text=tr(language, "back"), callback_data="back:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def preorder_confirmation_menu(
    language: str,
    product_id: int,
    quantity: int,
    base_unit_price: int,
) -> InlineKeyboardMarkup:
    confirm = "✅ Xác nhận đặt trước" if language == "vi" else "✅ Confirm preorder"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=confirm,
                    callback_data=(
                        f"preorder:confirm:{product_id}:{quantity}:{base_unit_price}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text=tr(language, "back"), callback_data="menu:preorders"
                )
            ],
        ]
    )


def preorder_history_menu(
    preorders: list[Preorder],
    language: str,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for preorder in preorders:
        name = sanitize_customer_text(
            preorder.product_name_en if language == "en" else preorder.product_name_vi
        )
        status = {
            "pending": "Đang chờ" if language == "vi" else "Pending",
            "processing": "Đang giao" if language == "vi" else "Processing",
            "completed": "Đã giao" if language == "vi" else "Delivered",
            "cancelled": "Đã hủy" if language == "vi" else "Cancelled",
        }.get(preorder.status, preorder.status)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{preorder.code} · {name} · {status}",
                    callback_data=f"preorder:detail:{preorder.id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="📦 Đặt trước thêm" if language == "vi" else "📦 New preorder",
                callback_data="menu:preorders",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text=tr(language, "back"), callback_data="back:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def preorder_detail_menu(preorder: Preorder, language: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if preorder.status == "pending":
        rows.append(
            [
                InlineKeyboardButton(
                    text="❌ Hủy đơn đặt trước"
                    if language == "vi"
                    else "❌ Cancel preorder",
                    callback_data=f"preorder:cancel:{preorder.id}",
                )
            ]
        )
    if preorder.completed_order_code:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📦 Mở đơn đã giao" if language == "vi" else "📦 Open delivery",
                    callback_data=f"preorder:order:{preorder.id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=tr(language, "back"), callback_data="preorder:history"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sms_rental_menu(
    language: str,
    price: int = 0,
    stock: int = 0,
    *,
    connected: bool = False,
    sources: list[SmsRentalSourceButton] | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if sources is not None:
        for source in sources:
            if not source.connected or source.stock <= 0:
                continue
            country = source.country_vi if language == "vi" else source.country_en
            flag = "🇺🇸" if source.key == "1" else "🇰🇭"
            rent_label = (
                f"{flag} Thuê số {country} · {format_vnd(source.price)}"
                if language == "vi"
                else f"{flag} Rent {country} number · {format_vnd(source.price)}"
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        text=rent_label,
                        callback_data=f"sms:rent:{source.key}",
                    )
                ]
            )
    elif connected and stock > 0:
        rent_label = (
            f"📲 Thuê số ngay · {format_vnd(price)}"
            if language == "vi"
            else f"📲 Rent now · {format_vnd(price)}"
        )
        rows.append([InlineKeyboardButton(text=rent_label, callback_data="sms:rent")])
    rows.append(
        [
            InlineKeyboardButton(
                text="🧾 Lịch sử thuê" if language == "vi" else "🧾 Rental history",
                callback_data="sms:history",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text=tr(language, "back"), callback_data="back:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sms_waiting_menu(
    language: str,
    price: int,
    source_key: str | None = None,
) -> InlineKeyboardMarkup:
    rent_label = (
        f"📲 Thuê số khác · {format_vnd(price)}"
        if language == "vi"
        else f"📲 Rent another · {format_vnd(price)}"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=rent_label,
                    callback_data=(
                        f"sms:rent:{source_key}" if source_key else "sms:rent"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🧾 Lịch sử thuê" if language == "vi" else "🧾 Rental history",
                    callback_data="sms:history",
                )
            ],
            [InlineKeyboardButton(text=tr(language, "back"), callback_data="back:menu")],
        ]
    )


def warehouse_api_menu(
    language: str,
    active: bool,
    docs_url: str,
    admin_blocked: bool = False,
) -> InlineKeyboardMarkup:
    rotate_text = "🔄 Đổi API Secret" if language == "vi" else "🔄 Rotate API Secret"
    toggle_text = (
        "⛔ Tạm khóa API" if active and language == "vi"
        else "✅ Mở lại API" if language == "vi"
        else "⛔ Disable API" if active
        else "✅ Enable API"
    )
    guide_text = "📘 Hướng dẫn đấu kho" if language == "vi" else "📘 Integration guide"
    toggle_button = (
        InlineKeyboardButton(
            text="🔒 Admin đã khóa" if language == "vi" else "🔒 Suspended by admin",
            callback_data="warehouse-api:blocked",
        )
        if admin_blocked
        else InlineKeyboardButton(text=toggle_text, callback_data="warehouse-api:toggle")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=guide_text, url=docs_url)],
            [InlineKeyboardButton(text=rotate_text, callback_data="warehouse-api:rotate")],
            [toggle_button],
            [InlineKeyboardButton(text=tr(language, "back"), callback_data="back:menu")],
        ]
    )


def warehouse_api_rotate_confirmation(language: str) -> InlineKeyboardMarkup:
    confirm = "✅ Xác nhận đổi Secret" if language == "vi" else "✅ Confirm rotation"
    cancel = "❌ Hủy" if language == "vi" else "❌ Cancel"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=confirm, callback_data="warehouse-api:rotate-confirm")],
            [InlineKeyboardButton(text=cancel, callback_data="menu:warehouse-api")],
        ]
    )


def referral_menu(language: str, referral_url: str) -> InlineKeyboardMarkup:
    share_text = (
        "Mua tài khoản tự động tại PHP Tool Shop và nhận hàng ngay."
        if language == "vi"
        else "Buy digital accounts automatically from PHP Tool Shop."
    )
    share_url = f"https://t.me/share/url?url={quote(referral_url)}&text={quote(share_text)}"
    label = "📤 Chia sẻ link mời" if language == "vi" else "📤 Share referral link"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, url=share_url)],
            [InlineKeyboardButton(text=tr(language, "back"), callback_data="back:menu")],
        ]
    )


def categories_menu(categories: list[Category], language: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for category in categories:
        name = sanitize_customer_text(category.name_en if language == "en" else category.name_vi)
        normalized = f"{category.name_vi} {category.name_en}".lower()
        if "api codex" in normalized:
            name = tr(language, "codex_api")
        elif "gemini" in normalized or "veo3" in normalized:
            name = "💎 GG Pro 18M"
        elif "gpt" in normalized or "chatgpt" in normalized:
            name = "🤖 Tài khoản GPT" if language == "vi" else "🤖 GPT accounts"
        builder.button(text=name, callback_data=f"cat:{category.id}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text=tr(language, "back"), callback_data="back:menu"))
    return builder.as_markup()


def codex_products_menu(
    products: list[Product],
    language: str,
    prices: dict[int, int] | None = None,
    usd_to_vnd: int | None = None,
) -> InlineKeyboardMarkup:
    return products_menu(
        products,
        language,
        "back:menu",
        prices,
        origin="codex",
        usd_to_vnd=usd_to_vnd,
    )


def products_menu(
    products: list[Product],
    language: str,
    back_callback: str,
    prices: dict[int, int] | None = None,
    *,
    origin: str | None = None,
    usd_to_vnd: int | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for product in products:
        name = sanitize_customer_text(product.name_en if language == "en" else product.name_vi)
        display_price = (prices or {}).get(product.id, product.price)
        menu_stock = max(
            0,
            int(getattr(product, "_menu_stock", product.external_stock) or 0),
        )
        in_stock = not product.force_out_of_stock and menu_stock > 0
        stock_label = "Hết hàng" if language == "vi" else "Out of stock"
        price_text = format_vnd(display_price)
        if usd_to_vnd:
            price_text += f" ({format_usd_from_vnd(display_price, usd_to_vnd)})"
        button_text = f"{name} · {price_text}"
        if not in_stock:
            button_text = f"🔴 {button_text} · {stock_label}"
        builder.button(
            text=button_text,
            callback_data=(
                f"prod:{product.id}:{origin}" if origin else f"prod:{product.id}"
            ),
            style="success" if in_stock else "danger",
        )
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text=tr(language, "back"), callback_data=back_callback))
    return builder.as_markup()


def product_detail(
    product: Product,
    language: str,
    stock: int,
    *,
    allow_coupon: bool = True,
    flash_sale_id: int | None = None,
    origin: str | None = None,
) -> InlineKeyboardMarkup:
    buy_callback = f"qtymenu:{product.id}" if product.allow_quantity else f"buy:{product.id}:1"
    if flash_sale_id is not None:
        buy_callback += f":flash:{flash_sale_id}"
    if product.allow_quantity and origin:
        buy_callback += f":origin:{origin}"
    rows = []
    if stock > 0:
        rows.append([InlineKeyboardButton(text=tr(language, "buy"), callback_data=buy_callback)])
        if allow_coupon:
            coupon_label = "🏷 Nhập mã giảm giá" if language == "vi" else "🏷 Apply discount code"
            rows.append(
                [InlineKeyboardButton(text=coupon_label, callback_data=f"coupon:{product.id}")]
            )
    back_callback = (
        "menu:quick"
        if origin == "quick"
        else "menu:codex-api"
        if origin == "codex"
        else f"cat:{product.category_id}"
    )
    rows.append(
        [
            InlineKeyboardButton(
                text=tr(language, "back"), callback_data=back_callback
            )
        ]
    )
    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def quantity_menu(
    product: Product,
    language: str,
    stock: int,
    unit_price: int | None = None,
    flash_sale_id: int | None = None,
    origin: str | None = None,
    variable_price: bool = False,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    maximum = min(product.max_quantity, max(0, stock))
    suggestions = [value for value in (1, 2, 5, 10) if value <= maximum]
    display_price = product.price if unit_price is None else unit_price
    for quantity in suggestions:
        callback_data = f"buy:{product.id}:{quantity}"
        if flash_sale_id is not None:
            callback_data += f":flash:{flash_sale_id}"
        builder.button(
            text=(
                f"{quantity} tài khoản · xem tổng"
                if variable_price and language == "vi"
                else f"{quantity} accounts · view total"
                if variable_price
                else f"{quantity} × {format_vnd(display_price)}"
            ),
            callback_data=callback_data,
        )
    builder.adjust(2)
    custom_label = "✍️ Nhập số lượng" if language == "vi" else "✍️ Custom quantity"
    if maximum > 0:
        custom_callback = f"customqty:{product.id}"
        if flash_sale_id is not None:
            custom_callback += f":flash:{flash_sale_id}"
        builder.row(
            InlineKeyboardButton(
                text=custom_label,
                callback_data=custom_callback,
            )
        )
    builder.row(
        InlineKeyboardButton(
            text=tr(language, "back"),
            callback_data=(
                f"prod:{product.id}:{origin}" if origin else f"prod:{product.id}"
            ),
        )
    )
    return builder.as_markup()


def seller_purchase_confirmation_menu(
    product_id: int,
    quantity: int,
    total_amount: int,
    language: str,
    flash_sale_id: int | None = None,
) -> InlineKeyboardMarkup:
    callback_data = f"sellerbuy:{product_id}:{quantity}:{total_amount}"
    if flash_sale_id is not None:
        callback_data += f":flash:{flash_sale_id}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=(
                        "✅ Xác nhận mua bằng ví"
                        if language == "vi"
                        else "✅ Confirm wallet purchase"
                    ),
                    callback_data=callback_data,
                )
            ],
            [
                InlineKeyboardButton(
                    text=tr(language, "back"),
                    callback_data=f"qtymenu:{product_id}",
                )
            ],
        ]
    )


def coupon_quantity_menu(
    product: Product,
    language: str,
    stock: int,
    coupon_id: int,
    final_unit_price: int,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    maximum = min(product.max_quantity if product.allow_quantity else 1, max(0, stock))
    suggestions = [value for value in (1, 2, 5, 10) if value <= maximum]
    for quantity in suggestions:
        builder.button(
            text=f"{quantity} × {format_vnd(final_unit_price)}",
            callback_data=f"buycoupon:{product.id}:{quantity}:{coupon_id}",
        )
    builder.adjust(2)
    if product.allow_quantity and maximum > 0:
        custom_label = "✍️ Nhập số lượng" if language == "vi" else "✍️ Custom quantity"
        builder.row(
            InlineKeyboardButton(
                text=custom_label,
                callback_data=f"customcouponqty:{product.id}:{coupon_id}",
            )
        )
    builder.row(
        InlineKeyboardButton(
            text=tr(language, "back"),
            callback_data=f"prod:{product.id}",
        )
    )
    return builder.as_markup()


def purchase_payment_options(
    product_id: int,
    quantity: int,
    language: str,
    coupon_id: int | None = None,
    flash_sale_id: int | None = None,
) -> InlineKeyboardMarkup:
    direct_label = "💳 Thanh toán QR cho đơn này" if language == "vi" else "💳 Pay this order by QR"
    direct_callback = f"directpay:{product_id}:{quantity}"
    if coupon_id is not None:
        direct_callback += f":{coupon_id}"
    elif flash_sale_id is not None:
        direct_callback += f":flash:{flash_sale_id}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=direct_label,
                    callback_data=direct_callback,
                )
            ],
            [
                InlineKeyboardButton(
                    text=tr(language, "deposit"),
                    callback_data="menu:deposit",
                )
            ],
            [
                InlineKeyboardButton(
                    text=tr(language, "back"),
                    callback_data=f"prod:{product_id}",
                )
            ],
        ]
    )


def deposit_amounts(language: str) -> InlineKeyboardMarkup:
    return deposit_amounts_for_providers(language, sepay_enabled=True)


def deposit_amounts_for_providers(
    language: str,
    *,
    sepay_enabled: bool = True,
    binance_enabled: bool = False,
    usd_to_vnd: int = 27_500,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for amount in (50_000, 100_000, 200_000, 500_000):
        if sepay_enabled:
            builder.button(
                text=f"🏦 {format_vnd(amount)}",
                callback_data=f"deposit:sepay:{amount}",
            )
        if binance_enabled:
            builder.button(
                text=f"₿ {format_usd_from_vnd(amount, usd_to_vnd)}",
                callback_data=f"deposit:binance:{amount}",
            )
    builder.adjust(2)
    if sepay_enabled:
        builder.button(
            text="🏦 Nhập số tiền khác" if language == "vi" else "🏦 Other bank amount",
            callback_data="deposit:sepay:other",
        )
    if binance_enabled:
        builder.button(
            text=(
                "₿ Nhập số tiền Binance khác"
                if language == "vi"
                else "₿ Other Binance amount"
            ),
            callback_data="deposit:binance:other",
        )
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text=tr(language, "back"), callback_data="back:menu"))
    return builder.as_markup()


def language_menu(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Tiếng Việt", callback_data="lang:vi"),
                InlineKeyboardButton(text="English", callback_data="lang:en"),
            ],
            [InlineKeyboardButton(text=tr(language, "back"), callback_data="back:menu")],
        ]
    )


def back_menu(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=tr(language, "back"), callback_data="back:menu")]
        ]
    )


def order_history_menu(orders: list[Order], language: str) -> InlineKeyboardMarkup:
    groups: dict[str, list[Order]] = {}
    for order in orders:
        key = order.batch_code or f"order:{order.id}"
        groups.setdefault(key, []).append(order)

    builder = InlineKeyboardBuilder()
    for grouped_orders in list(groups.values())[:10]:
        representative = min(grouped_orders, key=lambda item: item.id)
        name = sanitize_customer_text(
            representative.display_name_en
            if language == "en"
            else representative.display_name_vi
        )
        quantity = len(grouped_orders)
        quantity_label = "tài khoản" if language == "vi" else "items"
        builder.button(
            text=f"{representative.shop_order_code} · {name} · {quantity} {quantity_label}",
            callback_data=f"orderdetail:{representative.id}",
        )
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text=tr(language, "back"), callback_data="back:menu"))
    return builder.as_markup()
