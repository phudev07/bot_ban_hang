from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from urllib.parse import quote

from app.i18n import tr
from app.models import Category, Order, Product
from app.utils import format_vnd


def main_menu(language: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=tr(language, "quick"), callback_data="menu:quick"),
        InlineKeyboardButton(text=tr(language, "deposit"), callback_data="menu:deposit"),
    )
    builder.row(
        InlineKeyboardButton(text=tr(language, "codes"), callback_data="menu:codes"),
        InlineKeyboardButton(text=tr(language, "products"), callback_data="menu:products"),
    )
    builder.row(InlineKeyboardButton(text=tr(language, "sms"), callback_data="menu:sms"))
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


def sms_rental_menu(
    language: str,
    price: int,
    stock: int,
    *,
    connected: bool,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if connected and stock > 0:
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


def sms_waiting_menu(language: str, price: int) -> InlineKeyboardMarkup:
    rent_label = (
        f"📲 Thuê số khác · {format_vnd(price)}"
        if language == "vi"
        else f"📲 Rent another · {format_vnd(price)}"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=rent_label, callback_data="sms:rent")],
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
        name = category.name_en if language == "en" else category.name_vi
        builder.button(text=name, callback_data=f"cat:{category.id}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text=tr(language, "back"), callback_data="back:menu"))
    return builder.as_markup()


def products_menu(
    products: list[Product], language: str, back_callback: str
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for product in products:
        name = product.name_en if language == "en" else product.name_vi
        builder.button(
            text=f"{name} · {format_vnd(product.price)}", callback_data=f"prod:{product.id}"
        )
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text=tr(language, "back"), callback_data=back_callback))
    return builder.as_markup()


def product_detail(product: Product, language: str, stock: int) -> InlineKeyboardMarkup:
    buy_callback = f"qtymenu:{product.id}" if product.allow_quantity else f"buy:{product.id}:1"
    rows = []
    if stock > 0:
        rows.append([InlineKeyboardButton(text=tr(language, "buy"), callback_data=buy_callback)])
        coupon_label = "🏷 Nhập mã giảm giá" if language == "vi" else "🏷 Apply discount code"
        rows.append(
            [InlineKeyboardButton(text=coupon_label, callback_data=f"coupon:{product.id}")]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=tr(language, "back"), callback_data=f"cat:{product.category_id}"
            )
        ]
    )
    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def quantity_menu(product: Product, language: str, stock: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    maximum = min(product.max_quantity, max(0, stock))
    suggestions = [value for value in (1, 2, 5, 10) if value <= maximum]
    for quantity in suggestions:
        builder.button(
            text=f"{quantity} × {format_vnd(product.price)}",
            callback_data=f"buy:{product.id}:{quantity}",
        )
    builder.adjust(2)
    custom_label = "✍️ Nhập số lượng" if language == "vi" else "✍️ Custom quantity"
    if maximum > 0:
        builder.row(
            InlineKeyboardButton(
                text=custom_label,
                callback_data=f"customqty:{product.id}",
            )
        )
    builder.row(
        InlineKeyboardButton(
            text=tr(language, "back"),
            callback_data=f"prod:{product.id}",
        )
    )
    return builder.as_markup()


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
) -> InlineKeyboardMarkup:
    direct_label = "💳 Thanh toán QR cho đơn này" if language == "vi" else "💳 Pay this order by QR"
    direct_callback = f"directpay:{product_id}:{quantity}"
    if coupon_id is not None:
        direct_callback += f":{coupon_id}"
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
    builder = InlineKeyboardBuilder()
    for amount in (50_000, 100_000, 200_000, 500_000):
        builder.button(text=format_vnd(amount), callback_data=f"deposit:{amount}")
    builder.adjust(2)
    builder.row(
        InlineKeyboardButton(text=tr(language, "other_amount"), callback_data="deposit:other")
    )
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
        name = (
            representative.product.name_en
            if language == "en"
            else representative.product.name_vi
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
