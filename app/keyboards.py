from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

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
    builder.row(
        InlineKeyboardButton(text=tr(language, "orders"), callback_data="menu:orders"),
        InlineKeyboardButton(text=tr(language, "profile"), callback_data="menu:profile"),
    )
    builder.row(
        InlineKeyboardButton(text=tr(language, "support"), callback_data="menu:support"),
        InlineKeyboardButton(text=tr(language, "clear"), callback_data="menu:clear"),
    )
    builder.row(InlineKeyboardButton(text=tr(language, "language"), callback_data="menu:language"))
    return builder.as_markup()


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
