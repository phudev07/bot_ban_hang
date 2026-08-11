import asyncio
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.filters.command import CommandObject
from aiogram.filters.exception import ExceptionMessageFilter, ExceptionTypeFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ErrorEvent, Message, ReplyKeyboardRemove
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.autosms import AutoSmsClient
from app.chat_cleanup import delete_recent_messages
from app.config import Settings
from app.custom_emoji import product_brand_emoji
from app.database import release_session_connection
from app.delivery import (
    delivery_file,
    delivery_keyboard,
    delivery_text,
)
from app.flash_sales import (
    FlashSaleUnavailable,
    active_flash_sale_prices,
    flash_sale_remaining,
)
from app.haji_suppliers import HajiClient
from app.keyboards import (
    back_menu,
    categories_menu,
    coupon_quantity_menu,
    deposit_amounts,
    language_menu,
    main_menu,
    order_history_menu,
    preorder_confirmation_menu,
    preorder_detail_menu,
    preorder_history_menu,
    preorder_products_menu,
    product_detail,
    products_menu,
    purchase_payment_options,
    quick_access_keyboard,
    quantity_menu,
    referral_menu,
    seller_purchase_confirmation_menu,
    SmsRentalSourceButton,
    sms_rental_menu,
    sms_waiting_menu,
    warehouse_api_menu,
    warehouse_api_rotate_confirmation,
)
from app.lehai_suppliers import LeHaiPremiumClient
from app.maintenance import sms_rental_maintenance_enabled
from app.models import ApiClient, Order, Product, QuantityDiscount, User
from app.partner_services import ensure_api_client, referral_stats, rotate_api_secret
from app.payment_expiry import register_deposit_message
from app.product_tutorials import send_purchase_tutorials
from app.preorders import (
    PreorderError,
    cancel_user_preorder,
    create_preorder,
    preorder_detail_text,
    preorder_quote,
    preorderable_products,
    recent_user_preorders,
    user_preorder,
)
from app.rentsim import RentSimClient
from app.services import (
    active_categories,
    active_products,
    active_quantity_discounts,
    available_stock,
    CouponValidationError,
    create_deposit,
    customer_product_prices,
    ensure_user,
    local_inventory_stock,
    order_bundle,
    PendingDepositLimitReached,
    product_checkout_quote,
    product_pricing,
    purchase_quantity_limit,
    purchase_product,
    recent_orders,
    user_activity_stats,
)
from app.sms_customer_messages import rental_failure_text, storefront_text
from app.sms_rentals import (
    attach_sms_rental_message,
    attach_sms_waiting_message,
    recent_sms_rentals,
    rent_sms_number,
    sms_availability,
    sms_country_name,
)
from app.states import DepositStates, PreorderStates, PurchaseStates
from app.suppliers import (
    EXTERNAL_FULFILLMENT_SOURCES,
    ExternalSupplierClient,
    SumistoreClient,
)
from app.utils import (
    SecretCipher,
    build_sepay_qr_url,
    format_vnd,
    parse_vnd,
    safe_customer_html,
    safe_customer_telegram_html,
    sanitize_customer_text,
)


COUPON_ERROR_MESSAGES = {
    "coupon_empty": (
        "Bạn chưa nhập mã giảm giá.",
        "Please enter a discount code.",
    ),
    "coupon_not_found": (
        "Mã giảm giá không tồn tại.",
        "This discount code does not exist.",
    ),
    "coupon_wrong_product": (
        "Mã giảm giá không áp dụng cho sản phẩm này.",
        "This discount code does not apply to this product.",
    ),
    "coupon_inactive": (
        "Mã giảm giá hiện đang bị tắt.",
        "This discount code is currently disabled.",
    ),
    "coupon_not_started": (
        "Mã giảm giá chưa đến thời gian sử dụng.",
        "This discount code is not active yet.",
    ),
    "coupon_expired": (
        "Mã giảm giá đã hết hạn.",
        "This discount code has expired.",
    ),
    "coupon_already_used": (
        "Bạn đã sử dụng mã giảm giá này rồi.",
        "You have already used this discount code.",
    ),
    "coupon_exhausted": (
        "Mã giảm giá đã hết lượt sử dụng.",
        "This discount code has no uses remaining.",
    ),
    "coupon_seller_price": (
        "Giá seller đã là giá riêng theo giá vốn nên không cộng thêm mã giảm giá.",
        "Seller pricing follows source cost and cannot be stacked with coupons.",
    ),
}


def quantity_tier_offer_text(
    tier: QuantityDiscount,
    unit_price: int,
    language: str,
) -> str:
    if tier.discount_type == "fixed":
        final_price = max(1, int(unit_price) - max(0, int(tier.discount_amount)))
        return (
            f"Mua từ <b>{tier.min_quantity}+</b> nick giảm còn "
            f"<b>{format_vnd(final_price)}/1</b>"
            if language == "vi"
            else f"Buy <b>{tier.min_quantity}+</b> accounts for "
            f"<b>{format_vnd(final_price)} each</b>"
        )
    return (
        f"Mua từ <b>{tier.min_quantity}+</b> nick giảm "
        f"<b>{tier.discount_percent}%</b>"
        if language == "vi"
        else f"Buy <b>{tier.min_quantity}+</b> accounts and save "
        f"<b>{tier.discount_percent}%</b>"
    )


def unit_price_breakdown(unit_prices: tuple[int, ...]) -> tuple[tuple[int, int], ...]:
    grouped: dict[int, int] = {}
    for price in unit_prices:
        grouped[int(price)] = grouped.get(int(price), 0) + 1
    return tuple((quantity, price) for price, quantity in grouped.items())


def unit_price_breakdown_text(unit_prices: tuple[int, ...]) -> str:
    return " + ".join(
        f"<b>{quantity}</b> × <b>{format_vnd(price)}</b>"
        for quantity, price in unit_price_breakdown(unit_prices)
    )


def applied_quantity_discount_text(
    discount_type: str | None,
    discount_value: int,
    language: str,
) -> str:
    if discount_type == "fixed":
        return (
            f"giảm <b>{format_vnd(discount_value)}/1</b>"
            if language == "vi"
            else f"<b>{format_vnd(discount_value)} off each</b>"
        )
    return (
        f"<b>-{discount_value}%</b>"
        if language == "vi"
        else f"<b>{discount_value}% off</b>"
    )


def coupon_error_message(code: str, language: str) -> str:
    messages = COUPON_ERROR_MESSAGES.get(code)
    if messages is None:
        return (
            "Không thể áp dụng mã giảm giá. Vui lòng thử lại."
            if language == "vi"
            else "The discount code could not be applied. Please try again."
        )
    return messages[0] if language == "vi" else messages[1]


def home_text(user: User, settings: Settings) -> str:
    username = f"@{escape(user.username)}" if user.username else "Chưa đặt"
    group_url = escape(settings.community_group_url, quote=True)
    group_line_en = (
        f'\n📢 Telegram group: <a href="{group_url}">Join group</a>' if group_url else ""
    )
    group_line_vi = f'\n📢 Nhóm Telegram: <a href="{group_url}">Vào nhóm</a>' if group_url else ""
    if user.language == "en":
        return (
            f"✨ <b>Hello, {escape(user.full_name)}</b>\n\n"
            f"🧾 ID: <code>{user.telegram_id}</code>\n"
            f"👤 Username: {username}\n"
            f"👛 Available balance: <b>{format_vnd(user.balance)}</b>\n\n"
            "⚡ <b>Quick access</b>\n"
            "🛒 Quick buy · 💳 Deposit · 🔑 My codes\n"
            "⌨️ The three buttons below the chat are always ready.\n\n"
            f"💬 Support: @{escape(settings.support_username)}"
            f"{group_line_en}"
        )
    return (
        f"✨ <b>Xin chào, {escape(user.full_name)}</b>\n\n"
        f"🧾 ID: <code>{user.telegram_id}</code>\n"
        f"👤 Username: {username}\n"
        f"👛 Số dư khả dụng: <b>{format_vnd(user.balance)}</b>\n\n"
        "⚡ <b>Truy cập nhanh</b>\n"
        "🛒 Mua nhanh · 💳 Nạp tiền · 🔑 Lấy code\n"
        "⌨️ Ba nút dưới ô chat luôn sẵn sàng thao tác.\n\n"
        f"💬 Hỗ trợ: @{escape(settings.support_username)}"
        f"{group_line_vi}"
    )


async def get_or_create_user(
    message_or_callback: Message | CallbackQuery,
    session: AsyncSession,
    referral_code: str | None = None,
) -> User:
    telegram_user = message_or_callback.from_user
    if telegram_user is None:
        raise RuntimeError("Telegram update has no user")
    user = await ensure_user(session, telegram_user, referral_code)
    await session.commit()
    return user


async def edit_or_send_text(message: Message, text: str, **kwargs) -> Message:
    if message.text is None:
        return await message.answer(text, **kwargs)
    return await message.edit_text(text, **kwargs)


async def send_home_with_navigation(
    message: Message,
    user: User,
    settings: Settings,
    *,
    sms_enabled: bool,
) -> None:
    quick_access_text = (
        "⌨️ <b>Phím thao tác nhanh đã sẵn sàng</b>"
        if user.language == "vi"
        else "⌨️ <b>Quick actions are ready</b>"
    )
    refresh_message = await message.answer(
        "🔄 Đang làm mới bàn phím…"
        if user.language == "vi"
        else "🔄 Refreshing keyboard…",
        reply_markup=ReplyKeyboardRemove(),
    )
    await asyncio.sleep(0.15)
    await message.answer(
        quick_access_text,
        reply_markup=quick_access_keyboard(user.language),
    )
    try:
        await refresh_message.delete()
    except TelegramBadRequest:
        pass
    await message.answer(
        home_text(user, settings),
        reply_markup=main_menu(
            user.language,
            sms_enabled=sms_enabled,
        ),
        disable_web_page_preview=True,
    )


def create_router(
    settings: Settings,
    cipher: SecretCipher,
    supplier_client: SumistoreClient | None = None,
    lehai_client: LeHaiPremiumClient | None = None,
    rentsim_client: RentSimClient | None = None,
    canboso_client: ExternalSupplierClient | None = None,
    nce_client: ExternalSupplierClient | None = None,
    haji_client: HajiClient | None = None,
    autosms_client: AutoSmsClient | None = None,
) -> Router:
    router = Router(name="customer")
    bot_username_cache: str | None = None
    bot_username_lock = asyncio.Lock()
    warehouse_docs_url = (
        f"{settings.shop_api_base_url.rstrip('/').removesuffix('/v1')}/docs"
    )
    sms_sources = {
        key: value
        for key, value in (("1", autosms_client), ("855", rentsim_client))
        if value is not None
    }
    sms_enabled = bool(sms_sources)

    def sms_source_settings(source_key: str) -> tuple[int, int, int]:
        if source_key == "1":
            return (
                settings.autosms_markup,
                settings.autosms_fallback_price,
                settings.autosms_cooldown_seconds,
            )
        return (
            settings.rentsim_markup,
            settings.rentsim_fallback_price,
            settings.rentsim_cooldown_seconds,
        )

    async def current_sms_availabilities(*, force: bool = False):
        values = []
        for source_key, client in sms_sources.items():
            markup, fallback_price, _ = sms_source_settings(source_key)
            values.append(
                await sms_availability(
                    client,
                    markup,
                    fallback_unit_cost=fallback_price,
                    force=force,
                )
            )
        return values

    def sms_source_buttons(availabilities) -> list[SmsRentalSourceButton]:
        return [
            SmsRentalSourceButton(
                key=item.source_key,
                country_vi=item.country_vi,
                country_en=item.country_en,
                price=item.sale_price,
                stock=item.effective_stock,
                connected=item.connected,
            )
            for item in availabilities
        ]

    async def bot_username(bot: Bot) -> str:
        nonlocal bot_username_cache
        if bot_username_cache:
            return bot_username_cache
        async with bot_username_lock:
            if not bot_username_cache:
                bot_user = await bot.get_me()
                bot_username_cache = bot_user.username or "phptool_bot"
        return bot_username_cache

    @router.error(
        ExceptionTypeFilter(TelegramBadRequest),
        ExceptionMessageFilter(
            r".*(?:message is not modified|query is too old and response timeout expired).*$"
        ),
    )
    async def ignore_stale_callback_error(_event: ErrorEvent) -> bool:
        return True

    def bundle_values(orders, user: User) -> tuple[list[int], str, str, list[str], int]:
        order_ids = [order.id for order in orders]
        shop_order_code = orders[0].shop_order_code
        product_name = sanitize_customer_text(
            orders[0].display_name_en if user.language == "en" else orders[0].display_name_vi
        )
        secrets = [cipher.decrypt(order.inventory_item.encrypted_secret) for order in orders]
        total_amount = sum(order.amount for order in orders)
        return order_ids, shop_order_code, product_name, secrets, total_amount

    async def profile_text(user: User, session: AsyncSession) -> str:
        username = f"@{escape(user.username)}" if user.username else "—"
        stats = await user_activity_stats(session, user.telegram_id)
        if user.language == "en":
            return (
                "👤 <b>Your profile</b>\n\n"
                f"• ID: <code>{user.telegram_id}</code>\n"
                f"• Username: {username}\n"
                f"• Balance: <b>{format_vnd(user.balance)}</b>\n"
                f"• Joined: {user.created_at:%d/%m/%Y}\n\n"
                "<b>Activity</b>\n"
                f"• Purchases: <b>{stats.purchase_count}</b>\n"
                f"• Items received: <b>{stats.purchased_items}</b>\n"
                f"• Successful deposits: <b>{stats.deposit_count}</b>\n"
                f"• Total spent: <b>{format_vnd(stats.total_spent)}</b>\n"
                f"• Total deposited: <b>{format_vnd(stats.total_deposited)}</b>"
            )
        return (
            "👤 <b>Hồ sơ của bạn</b>\n\n"
            f"• ID: <code>{user.telegram_id}</code>\n"
            f"• Username: {username}\n"
            f"• Số dư: <b>{format_vnd(user.balance)}</b>\n"
            f"• Tham gia: {user.created_at:%d/%m/%Y}\n\n"
            "<b>Thống kê hoạt động</b>\n"
            f"• Lượt mua hàng: <b>{stats.purchase_count}</b>\n"
            f"• Sản phẩm đã nhận: <b>{stats.purchased_items}</b>\n"
            f"• Lượt nạp thành công: <b>{stats.deposit_count}</b>\n"
            f"• Tổng tiền đã mua: <b>{format_vnd(stats.total_spent)}</b>\n"
            f"• Tổng tiền đã nạp: <b>{format_vnd(stats.total_deposited)}</b>"
        )

    async def send_quick_buy(message: Message, session: AsyncSession) -> None:
        user = await get_or_create_user(message, session)
        products = await active_products(session)
        flash_prices = await active_flash_sale_prices(
            session, [product.id for product in products]
        )
        display_prices = await customer_product_prices(
            session,
            products,
            user.telegram_id,
            flash_prices,
        )
        text = "⚡ <b>Mua nhanh</b>" if user.language == "vi" else "⚡ <b>Quick buy</b>"
        if not products:
            text = "Kho chưa có mặt hàng." if user.language == "vi" else "No products yet."
        await message.answer(
            text,
            reply_markup=products_menu(
                products,
                user.language,
                "back:menu",
                display_prices,
                origin="quick",
            ),
        )

    async def send_deposit_menu(message: Message, session: AsyncSession) -> None:
        user = await get_or_create_user(message, session)
        if not settings.sepay_enabled:
            text = (
                "💳 Chức năng nạp tiền đang được cấu hình. Vui lòng quay lại sau."
                if user.language == "vi"
                else "💳 Deposits are being configured. Please check again later."
            )
            await message.answer(text, reply_markup=back_menu(user.language))
            return
        text = (
            f"💳 <b>Nạp tiền tự động</b>\n\n"
            f"Chọn số tiền muốn nạp. Tối thiểu {format_vnd(settings.min_deposit)}."
            if user.language == "vi"
            else f"💳 <b>Automatic deposit</b>\n\n"
            f"Choose an amount. Minimum {format_vnd(settings.min_deposit)}."
        )
        await message.answer(text, reply_markup=deposit_amounts(user.language))

    async def send_preorder_menu(message: Message, session: AsyncSession) -> None:
        user = await get_or_create_user(message, session)
        products = await preorderable_products(session)
        if user.language == "en":
            text = (
                "📦 <b>Preorder out-of-stock products</b>\n\n"
                "The preorder price is the current normal price plus 5%. The full amount "
                "is charged when you confirm. If you cancel, or the product price changes "
                "before delivery, the amount is refunded automatically."
            )
            if not products:
                text += "\n\nThere are currently no out-of-stock products open for preorder."
        else:
            text = (
                "📦 <b>Đặt trước mặt hàng đang hết</b>\n\n"
                "Giá đặt trước bằng giá thường hiện tại cộng 5%. Bot trừ toàn bộ tiền ngay "
                "khi xác nhận. Nếu bạn hủy hoặc giá sản phẩm thay đổi trước lúc giao, tiền "
                "sẽ tự động hoàn lại ví."
            )
            if not products:
                text += "\n\nHiện không có mặt hàng hết kho nào đang nhận đặt trước."
        await message.answer(
            text,
            reply_markup=preorder_products_menu(products, user.language),
        )

    def preorder_error_message(code: str, language: str) -> str:
        vi_messages = {
            "not_found": "Không tìm thấy mặt hàng hoặc đơn đặt trước.",
            "blocked": "Tài khoản của bạn đang bị hạn chế giao dịch.",
            "not_available": "Mặt hàng này hiện không nhận đặt trước.",
            "price_changed": "Giá mặt hàng vừa thay đổi. Hãy mở lại mục đặt trước để xem giá mới.",
            "invalid_quantity": "Số lượng đặt trước không hợp lệ.",
            "in_stock": "Mặt hàng đã có hàng. Bạn có thể mua ngay trong mục Mua nhanh.",
            "duplicate": "Bạn đã có một đơn đang chờ cho mặt hàng này.",
            "active_limit": "Bạn đã đạt giới hạn đơn đặt trước đang chờ.",
            "insufficient": "Số dư ví hiện tại không đủ cho tổng tiền đặt trước.",
            "too_late": "Đơn đang được giao hoặc đã kết thúc nên không thể hủy.",
        }
        en_messages = {
            "not_found": "The product or preorder was not found.",
            "blocked": "Your account is currently restricted.",
            "not_available": "This product is not open for preorder.",
            "price_changed": "The price just changed. Reopen preorders to see the new price.",
            "invalid_quantity": "The preorder quantity is invalid.",
            "in_stock": "This product is back in stock. You can buy it from Quick buy.",
            "duplicate": "You already have an active preorder for this product.",
            "active_limit": "You have reached the active preorder limit.",
            "insufficient": "Your current wallet balance is insufficient for this preorder.",
            "too_late": "This preorder is being fulfilled or has ended and cannot be cancelled.",
        }
        messages = vi_messages if language == "vi" else en_messages
        return messages.get(code, "Không thể xử lý đơn đặt trước. Vui lòng thử lại.")

    @router.message(CommandStart())
    async def start(
        message: Message,
        session: AsyncSession,
        state: FSMContext,
        command: CommandObject,
    ) -> None:
        await state.clear()
        referral_code = None
        if command.args and command.args.lower().startswith("ref_"):
            referral_code = command.args[4:]
        user = await get_or_create_user(message, session, referral_code)
        user.has_started = True
        await session.commit()
        await send_home_with_navigation(
            message,
            user,
            settings,
            sms_enabled=sms_enabled,
        )

    @router.message(Command("muanhanh"))
    async def quick_buy_command(message: Message, session: AsyncSession) -> None:
        await send_quick_buy(message, session)

    @router.message(Command("naptien"))
    async def deposit_command(message: Message, session: AsyncSession) -> None:
        await send_deposit_menu(message, session)

    @router.message(Command("dattruoc"))
    async def preorder_command(
        message: Message,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        await state.clear()
        await send_preorder_menu(message, session)

    @router.message(F.text.in_({"☰ Menu", "Menu"}))
    async def quick_menu_button(
        message: Message,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        await state.clear()
        user = await get_or_create_user(message, session)
        await message.answer(
            home_text(user, settings),
            reply_markup=main_menu(
                user.language,
                sms_enabled=sms_enabled,
            ),
            disable_web_page_preview=True,
        )

    @router.message(F.text.in_({"⚡ Mua nhanh", "⚡ Quick buy", "Mua nhanh", "Quick buy"}))
    async def quick_buy_button(
        message: Message,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        await state.clear()
        await send_quick_buy(message, session)

    @router.message(F.text.in_({"💳 Nạp tiền", "💳 Deposit", "Nạp tiền", "Deposit"}))
    async def deposit_button(
        message: Message,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        await state.clear()
        await send_deposit_menu(message, session)

    @router.message(Command("donmua"))
    async def orders_command(message: Message, session: AsyncSession) -> None:
        user = await get_or_create_user(message, session)
        orders = await recent_orders(session, user.telegram_id, limit=40)
        if not orders:
            text = (
                "📦 Bạn chưa có đơn mua nào." if user.language == "vi" else "📦 You have no orders."
            )
            markup = back_menu(user.language)
        else:
            text = (
                "📦 <b>Đơn hàng đã mua</b>\n\nChọn một đơn để xem tài khoản, sao chép "
                "hoặc tải lại file TXT."
                if user.language == "vi"
                else "📦 <b>Purchased orders</b>\n\nChoose an order to view, copy, or "
                "download its TXT file."
            )
            markup = order_history_menu(orders, user.language)
        await message.answer(text, reply_markup=markup)

    @router.message(Command("hoso"))
    async def profile_command(message: Message, session: AsyncSession) -> None:
        user = await get_or_create_user(message, session)
        await message.answer(
            await profile_text(user, session),
            reply_markup=back_menu(user.language),
        )

    @router.message(Command("hotro"))
    async def support_command(message: Message, session: AsyncSession) -> None:
        user = await get_or_create_user(message, session)
        text = (
            f"🆘 Cần hỗ trợ? Liên hệ @{escape(settings.support_username)} và gửi kèm mã đơn."
            if user.language == "vi"
            else f"🆘 Need help? Contact @{escape(settings.support_username)} with your order ID."
        )
        await message.answer(text, reply_markup=back_menu(user.language))

    @router.message(Command("donchat"))
    async def clear_chat_command(
        message: Message,
        bot: Bot,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        await state.clear()
        user = await get_or_create_user(message, session)
        await delete_recent_messages(
            bot,
            chat_id=message.chat.id,
            newest_message_id=message.message_id,
        )
        await bot.send_message(
            message.chat.id,
            home_text(user, settings),
            reply_markup=quick_access_keyboard(user.language),
            disable_web_page_preview=True,
        )

    @router.callback_query(F.data == "back:menu")
    async def back_to_menu(
        callback: CallbackQuery, session: AsyncSession, state: FSMContext
    ) -> None:
        await callback.answer()
        await state.clear()
        user = await get_or_create_user(callback, session)
        if callback.message:
            await edit_or_send_text(
                callback.message,
                home_text(user, settings),
                reply_markup=main_menu(
                    user.language,
                    sms_enabled=sms_enabled,
                ),
                disable_web_page_preview=True,
            )

    @router.callback_query(F.data == "menu:products")
    async def show_categories(callback: CallbackQuery, session: AsyncSession) -> None:
        await callback.answer()
        user = await get_or_create_user(callback, session)
        categories = await active_categories(session)
        text = (
            "🛍 <b>Chọn danh mục sản phẩm</b>"
            if user.language == "vi"
            else "🛍 <b>Choose a category</b>"
        )
        if not categories:
            text = "Kho chưa có danh mục." if user.language == "vi" else "No categories yet."
        if callback.message:
            await edit_or_send_text(
                callback.message,
                text, reply_markup=categories_menu(categories, user.language)
            )

    @router.callback_query(F.data.startswith("cat:"))
    async def show_products(callback: CallbackQuery, session: AsyncSession) -> None:
        await callback.answer()
        user = await get_or_create_user(callback, session)
        category_id = int(callback.data.split(":", 1)[1])
        products = await active_products(session, category_id)
        flash_prices = await active_flash_sale_prices(
            session, [product.id for product in products]
        )
        display_prices = await customer_product_prices(
            session,
            products,
            user.telegram_id,
            flash_prices,
        )
        text = "📦 <b>Chọn mặt hàng</b>" if user.language == "vi" else "📦 <b>Choose a product</b>"
        if not products:
            text = (
                "Danh mục này chưa có mặt hàng."
                if user.language == "vi"
                else "This category is empty."
            )
        if callback.message:
            await callback.message.edit_text(
                text,
                reply_markup=products_menu(
                    products,
                    user.language,
                    "menu:products",
                    display_prices,
                ),
            )

    @router.callback_query(F.data == "menu:quick")
    async def quick_buy(callback: CallbackQuery, session: AsyncSession) -> None:
        await callback.answer()
        user = await get_or_create_user(callback, session)
        products = await active_products(session)
        flash_prices = await active_flash_sale_prices(
            session, [product.id for product in products]
        )
        display_prices = await customer_product_prices(
            session,
            products,
            user.telegram_id,
            flash_prices,
        )
        text = "⚡ <b>Mua nhanh</b>" if user.language == "vi" else "⚡ <b>Quick buy</b>"
        if not products:
            text = "Kho chưa có mặt hàng." if user.language == "vi" else "No products yet."
        if callback.message:
            await callback.message.edit_text(
                text,
                reply_markup=products_menu(
                    products,
                    user.language,
                    "back:menu",
                    display_prices,
                    origin="quick",
                ),
            )

    @router.callback_query(F.data == "menu:preorders")
    async def show_preorders(
        callback: CallbackQuery,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        await callback.answer()
        await state.clear()
        user = await get_or_create_user(callback, session)
        products = await preorderable_products(session)
        if user.language == "en":
            text = (
                "📦 <b>Preorder out-of-stock products</b>\n\n"
                "Price: current normal price +5%. The amount is charged immediately and "
                "refunded automatically if the preorder is cancelled."
            )
            if not products:
                text += "\n\nNo out-of-stock products are open for preorder right now."
        else:
            text = (
                "📦 <b>Đặt trước mặt hàng đang hết</b>\n\n"
                "Giá: giá thường hiện tại +5%. Bot trừ tiền ngay khi xác nhận và tự hoàn "
                "đầy đủ nếu đơn đặt trước bị hủy."
            )
            if not products:
                text += "\n\nHiện không có mặt hàng hết kho nào đang nhận đặt trước."
        if callback.message:
            await edit_or_send_text(
                callback.message,
                text,
                reply_markup=preorder_products_menu(products, user.language),
            )

    @router.callback_query(F.data.startswith("preorder:product:"))
    async def choose_preorder_product(
        callback: CallbackQuery,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        user = await get_or_create_user(callback, session)
        try:
            product_id = int(callback.data.rsplit(":", 1)[1])
        except (TypeError, ValueError):
            await callback.answer("Mặt hàng không hợp lệ.", show_alert=True)
            return
        products = await preorderable_products(session)
        product = next((item for item in products if item.id == product_id), None)
        if product is None:
            await callback.answer(
                preorder_error_message("not_available", user.language),
                show_alert=True,
            )
            return
        maximum = max(1, int(product.max_quantity)) if product.allow_quantity else 1
        await state.set_state(PreorderStates.waiting_for_quantity)
        await state.update_data(preorder_product_id=product.id)
        name = sanitize_customer_text(
            product.name_en if user.language == "en" else product.name_vi
        )
        text = (
            f"📦 <b>Preorder {escape(name)}</b>\n\n"
            f"Enter the quantity from 1 to {maximum}."
            if user.language == "en"
            else f"📦 <b>Đặt trước {escape(name)}</b>\n\n"
            f"Nhập số lượng cần đặt từ 1 đến {maximum}."
        )
        if callback.message:
            await callback.message.edit_text(text, reply_markup=back_menu(user.language))
        await callback.answer()

    @router.message(PreorderStates.waiting_for_quantity)
    async def receive_preorder_quantity(
        message: Message,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        user = await get_or_create_user(message, session)
        raw_quantity = (message.text or "").strip()
        try:
            quantity = int(raw_quantity)
        except ValueError:
            await message.answer(
                "Vui lòng nhập số lượng bằng số nguyên."
                if user.language == "vi"
                else "Please enter a whole number."
            )
            return
        data = await state.get_data()
        product_id = int(data.get("preorder_product_id") or 0)
        products = await preorderable_products(session)
        product = next((item for item in products if item.id == product_id), None)
        if product is None:
            await state.clear()
            await message.answer(
                preorder_error_message("not_available", user.language),
                reply_markup=back_menu(user.language),
            )
            return
        maximum = max(1, int(product.max_quantity)) if product.allow_quantity else 1
        if quantity < 1 or quantity > maximum:
            await message.answer(
                f"Số lượng phải từ 1 đến {maximum}."
                if user.language == "vi"
                else f"Quantity must be between 1 and {maximum}."
            )
            return
        quote = preorder_quote(product, quantity)
        if int(user.balance) < quote.total_amount:
            await state.clear()
            text = (
                f"❌ <b>Số dư ví chưa đủ</b>\n\n"
                f"• Cần: <b>{format_vnd(quote.total_amount)}</b>\n"
                f"• Hiện có: <b>{format_vnd(user.balance)}</b>\n\n"
                "Hãy nạp thêm tiền rồi đặt lại. Bot chưa trừ tiền."
                if user.language == "vi"
                else f"❌ <b>Insufficient wallet balance</b>\n\n"
                f"• Required: <b>{format_vnd(quote.total_amount)}</b>\n"
                f"• Available: <b>{format_vnd(user.balance)}</b>\n\n"
                "Deposit funds and try again. No money was deducted."
            )
            await message.answer(text, reply_markup=back_menu(user.language))
            return
        await state.clear()
        name = sanitize_customer_text(
            product.name_en if user.language == "en" else product.name_vi
        )
        text = (
            f"📦 <b>Confirm preorder</b>\n\n"
            f"• Product: <b>{escape(name)}</b>\n"
            f"• Normal price: <b>{format_vnd(quote.base_unit_price)}/1</b>\n"
            f"• Preorder price (+5%): <b>{format_vnd(quote.preorder_unit_price)}/1</b>\n"
            f"• Quantity: <b>{quantity}</b>\n"
            f"• Expected total: <b>{format_vnd(quote.total_amount)}</b>\n"
            f"• Wallet: <b>{format_vnd(user.balance)}</b>\n\n"
            "Confirming will deduct the expected total from your wallet immediately."
            if user.language == "en"
            else f"📦 <b>Xác nhận đặt trước</b>\n\n"
            f"• Sản phẩm: <b>{escape(name)}</b>\n"
            f"• Giá thường: <b>{format_vnd(quote.base_unit_price)}/1</b>\n"
            f"• Giá đặt trước (+5%): <b>{format_vnd(quote.preorder_unit_price)}/1</b>\n"
            f"• Số lượng: <b>{quantity}</b>\n"
            f"• Tổng dự kiến: <b>{format_vnd(quote.total_amount)}</b>\n"
            f"• Số dư ví: <b>{format_vnd(user.balance)}</b>\n\n"
            "Bấm xác nhận sẽ trừ ngay tổng tiền trên khỏi ví."
        )
        await message.answer(
            text,
            reply_markup=preorder_confirmation_menu(
                user.language,
                product.id,
                quantity,
                quote.base_unit_price,
            ),
        )

    @router.callback_query(F.data.startswith("preorder:confirm:"))
    async def confirm_preorder(callback: CallbackQuery, session: AsyncSession) -> None:
        user = await get_or_create_user(callback, session)
        language = user.language
        try:
            _prefix, _confirm, product_id, quantity, base_price = callback.data.split(":")
            product_id_value = int(product_id)
            quantity_value = int(quantity)
            base_price_value = int(base_price)
        except (AttributeError, TypeError, ValueError):
            await callback.answer("Dữ liệu đặt trước không hợp lệ.", show_alert=True)
            return
        try:
            preorder = await create_preorder(
                session,
                user.telegram_id,
                product_id_value,
                quantity_value,
                expected_base_unit_price=base_price_value,
                max_active_per_user=settings.preorder_max_active_per_user,
            )
            await session.commit()
        except PreorderError as exc:
            if session.in_transaction():
                await session.rollback()
            await callback.answer(
                preorder_error_message(exc.code, language),
                show_alert=True,
            )
            return
        if callback.message:
            await callback.message.edit_text(
                preorder_detail_text(preorder, language),
                reply_markup=preorder_detail_menu(preorder, language),
            )
        await callback.answer(
            "Đặt trước thành công" if language == "vi" else "Preorder created"
        )

    @router.callback_query(F.data == "preorder:history")
    async def show_preorder_history(callback: CallbackQuery, session: AsyncSession) -> None:
        user = await get_or_create_user(callback, session)
        preorders = await recent_user_preorders(session, user.telegram_id)
        text = (
            "🧾 <b>Đơn đặt trước của tôi</b>\n\nChọn một đơn để xem trạng thái."
            if user.language == "vi"
            else "🧾 <b>My preorders</b>\n\nChoose a preorder to view its status."
        )
        if not preorders:
            text = (
                "🧾 Bạn chưa có đơn đặt trước nào."
                if user.language == "vi"
                else "🧾 You have no preorders yet."
            )
        if callback.message:
            await callback.message.edit_text(
                text,
                reply_markup=preorder_history_menu(preorders, user.language),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("preorder:detail:"))
    async def show_preorder_detail(callback: CallbackQuery, session: AsyncSession) -> None:
        user = await get_or_create_user(callback, session)
        preorder_id = int(callback.data.rsplit(":", 1)[1])
        preorder = await user_preorder(session, user.telegram_id, preorder_id)
        if preorder is None:
            await callback.answer(
                preorder_error_message("not_found", user.language), show_alert=True
            )
            return
        if callback.message:
            await callback.message.edit_text(
                preorder_detail_text(preorder, user.language),
                reply_markup=preorder_detail_menu(preorder, user.language),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("preorder:cancel:"))
    async def cancel_preorder_callback(
        callback: CallbackQuery,
        session: AsyncSession,
    ) -> None:
        user = await get_or_create_user(callback, session)
        preorder_id = int(callback.data.rsplit(":", 1)[1])
        try:
            preorder = await cancel_user_preorder(
                session,
                user.telegram_id,
                preorder_id,
            )
            await session.commit()
        except PreorderError as exc:
            await session.rollback()
            await callback.answer(
                preorder_error_message(exc.code, user.language), show_alert=True
            )
            return
        if callback.message:
            await callback.message.edit_text(
                preorder_detail_text(preorder, user.language),
                reply_markup=preorder_detail_menu(preorder, user.language),
            )
        await callback.answer(
            "Đã hủy và hoàn tiền về ví"
            if user.language == "vi"
            else "Cancelled and refunded"
        )

    @router.callback_query(F.data.startswith("preorder:order:"))
    async def open_preorder_order(callback: CallbackQuery, session: AsyncSession) -> None:
        user = await get_or_create_user(callback, session)
        preorder_id = int(callback.data.rsplit(":", 1)[1])
        preorder = await user_preorder(session, user.telegram_id, preorder_id)
        if preorder is None or preorder.status != "completed":
            await callback.answer(
                preorder_error_message("not_found", user.language), show_alert=True
            )
            return
        order_id = await session.scalar(
            select(Order.id)
            .where(
                Order.preorder_id == preorder.id,
                Order.user_id == user.telegram_id,
            )
            .order_by(Order.id)
            .limit(1)
        )
        if order_id is None:
            await callback.answer("Không tìm thấy đơn đã giao.", show_alert=True)
            return
        orders = await order_bundle(session, user.telegram_id, order_id)
        order_ids, shop_order_code, product_name, secrets, total_amount = bundle_values(
            orders, user
        )
        if callback.message:
            await callback.message.edit_text(
                delivery_text(
                    shop_order_code=shop_order_code,
                    product_name=product_name,
                    secrets=secrets,
                    total_amount=total_amount,
                    language=user.language,
                ),
                reply_markup=delivery_keyboard(
                    primary_order_id=min(order_ids),
                    secrets=secrets,
                    language=user.language,
                ),
            )
        await callback.answer()

    async def show_quick_group(
        callback: CallbackQuery,
        session: AsyncSession,
        group: str,
    ) -> None:
        await callback.answer()
        user = await get_or_create_user(callback, session)
        products = await active_products(session)
        def is_gg18m(product: Product) -> bool:
            name = f"{product.name_vi} {product.name_en}".lower()
            return "18m" in name and any(
                marker in name for marker in ("gg", "google", "gemini", "jio")
            )

        if group == "gg18m":
            selected = [product for product in products if is_gg18m(product)]
        else:
            selected = [
                product
                for product in products
                if not is_gg18m(product)
                and "gpt" in f"{product.name_vi} {product.name_en}".lower()
            ]
        flash_prices = await active_flash_sale_prices(
            session, [product.id for product in selected]
        )
        display_prices = await customer_product_prices(
            session,
            selected,
            user.telegram_id,
            flash_prices,
        )
        text = (
            "🤖 <b>Tài khoản GPT</b>"
            if group == "gpt" and user.language == "vi"
            else "🤖 <b>GPT accounts</b>"
            if group == "gpt"
            else "💎 <b>GG Pro 18M</b>"
        )
        if callback.message:
            await callback.message.edit_text(
                text,
                reply_markup=products_menu(
                    selected,
                    user.language,
                    "menu:quick",
                    display_prices,
                    origin="quick",
                ),
            )

    @router.callback_query(F.data.startswith("quick:"))
    async def quick_group(callback: CallbackQuery, session: AsyncSession) -> None:
        await show_quick_group(callback, session, callback.data.split(":", 1)[1])

    @router.callback_query(F.data == "menu:sms")
    async def show_sms_rental(callback: CallbackQuery, session: AsyncSession) -> None:
        await callback.answer()
        user = await get_or_create_user(callback, session)
        if await sms_rental_maintenance_enabled(session):
            text = (
                "🛠 <b>Dịch vụ thuê số đang bảo trì</b>\n\n"
                "Hiện tại bot tạm ngừng nhận lượt thuê số mới. Các đơn đã thuê trước đó "
                "vẫn tiếp tục được kiểm tra OTP và hoàn ví theo trạng thái từng số."
                if user.language == "vi"
                else "🛠 <b>SMS rentals are under maintenance</b>\n\n"
                "New rentals are temporarily paused. Existing rentals will still be checked "
                "for OTP and refunded according to each number's status."
            )
            if callback.message:
                await edit_or_send_text(
                    callback.message,
                    text,
                    reply_markup=sms_rental_menu(
                        user.language,
                        sources=[],
                    ),
                )
            return
        availabilities = await current_sms_availabilities()
        first = availabilities[0] if availabilities else None
        text = storefront_text(
            user.language,
            connected=any(item.connected for item in availabilities),
            sale_price=(
                first.sale_price
                if first is not None
                else settings.autosms_fallback_price + settings.autosms_markup
            ),
            effective_stock=sum(item.effective_stock for item in availabilities),
            sources=availabilities,
        )
        if callback.message:
            await edit_or_send_text(
                callback.message,
                text,
                reply_markup=sms_rental_menu(
                    user.language,
                    sources=sms_source_buttons(availabilities),
                ),
            )

    @router.callback_query(F.data.startswith("sms:rent"))
    async def rent_sms(
        callback: CallbackQuery,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await get_or_create_user(callback, session)
        if await sms_rental_maintenance_enabled(session):
            alert = (
                "Dịch vụ thuê số đang bảo trì, vui lòng quay lại sau."
                if user.language == "vi"
                else "SMS rentals are under maintenance. Please try again later."
            )
            await callback.answer(alert, show_alert=True)
            if callback.message:
                await edit_or_send_text(
                    callback.message,
                    (
                        "🛠 <b>Dịch vụ thuê số đang bảo trì</b>\n\n"
                        "Bot chưa nhận lượt thuê mới trong thời gian này."
                        if user.language == "vi"
                        else "🛠 <b>SMS rentals are under maintenance</b>\n\n"
                        "The bot is not accepting new rentals right now."
                    ),
                    reply_markup=sms_rental_menu(
                        user.language,
                        sources=[],
                    ),
                )
            return
        parts = (callback.data or "").split(":")
        source_key = parts[2] if len(parts) >= 3 else ""
        client = sms_sources.get(source_key)
        if client is None:
            availabilities = await current_sms_availabilities()
            selected = next(
                (
                    item
                    for item in availabilities
                    if item.connected and item.effective_stock > 0
                ),
                None,
            )
            if selected is None:
                await callback.answer(
                    (
                        "Hiện cả hai khu vực đều tạm hết số."
                        if user.language == "vi"
                        else "Both locations are currently out of numbers."
                    ),
                    show_alert=True,
                )
                if callback.message:
                    await edit_or_send_text(
                        callback.message,
                        storefront_text(
                            user.language,
                            connected=False,
                            sale_price=settings.autosms_fallback_price
                            + settings.autosms_markup,
                            effective_stock=0,
                            sources=availabilities,
                        ),
                        reply_markup=sms_rental_menu(
                            user.language,
                            sources=sms_source_buttons(availabilities),
                        ),
                    )
                return
            source_key = selected.source_key
            client = sms_sources[source_key]
        markup, _fallback_price, cooldown_seconds = sms_source_settings(source_key)
        country = sms_country_name(client.provider, user.language)
        await callback.answer("⏳ Đang lấy số...")
        loading_text = (
            f"⏳ <b>Getting a ChatGPT number with country code {escape(country)}...</b>\n\n"
            "The bot is reserving your rental. Please wait and do not tap the rent button again."
            if user.language == "en"
            else f"⏳ <b>Đang lấy số ChatGPT mã {escape(country)}...</b>\n\n"
            "Bot đang kết nối nguồn và giữ lượt thuê cho bạn. Vui lòng chờ, không bấm lại nút thuê số."
        )
        if callback.message:
            await edit_or_send_text(callback.message, loading_text, reply_markup=None)
        result = await rent_sms_number(
            session_factory,
            user.telegram_id,
            client,
            markup=markup,
            cooldown_seconds=cooldown_seconds,
            referral_commission_percent=settings.referral_commission_percent,
        )
        if not result.ok:
            text = rental_failure_text(
                user.language,
                message=result.message,
                status=result.status,
                sale_amount=result.sale_amount,
                balance=result.balance,
                retry_after=result.retry_after,
            )
            if callback.message:
                await edit_or_send_text(
                    callback.message,
                    text,
                    reply_markup=back_menu(user.language),
                )
            return

        phone_text = (
            "📲 <b>SMS number rented</b>\n\n"
            f"• Order: <code>{escape(result.shop_order_code or '')}</code>\n"
            "• Service: <b>ChatGPT</b>\n"
            f"• Country: <b>{escape(country)}</b>\n"
            f"• Number: <code>{escape(result.phone_number)}</code>\n"
            f"• Charged: <b>{format_vnd(result.sale_amount)}</b>\n"
            f"• Wallet balance: <b>{format_vnd(result.balance)}</b>"
            if user.language == "en"
            else "📲 <b>Đã thuê số nhận SMS</b>\n\n"
            f"• Mã đơn: <code>{escape(result.shop_order_code or '')}</code>\n"
            "• Dịch vụ: <b>ChatGPT</b>\n"
            f"• Khu vực: <b>{escape(country)}</b>\n"
            f"• Số điện thoại: <code>{escape(result.phone_number)}</code>\n"
            f"• Đã trừ: <b>{format_vnd(result.sale_amount)}</b>\n"
            f"• Số dư ví: <b>{format_vnd(result.balance)}</b>"
        )
        if callback.message:
            rental_message = await edit_or_send_text(callback.message, phone_text)
            if result.status == "pending":
                await attach_sms_rental_message(
                    session_factory,
                    result.rental_id or 0,
                    user.telegram_id,
                    rental_message.message_id,
                )
        if result.status == "success":
            otp_text = (
                "✅ <b>OTP received</b>\n\n"
                f"• Code: <code>{escape(result.otp_code or '—')}</code>\n"
                f"• Message: {safe_customer_html(result.otp_content or '—')}\n\n"
                "You can rent another number after 60 seconds from this rental."
                if user.language == "en"
                else "✅ <b>Đã nhận được OTP</b>\n\n"
                f"• Mã OTP: <code>{escape(result.otp_code or '—')}</code>\n"
                f"• Nội dung: {safe_customer_html(result.otp_content or '—')}\n\n"
                "Bạn có thể thuê số tiếp theo sau khi đủ 60 giây tính từ lượt thuê này."
            )
            if callback.message:
                await callback.message.answer(
                    otp_text,
                    reply_markup=sms_waiting_menu(
                        user.language,
                        result.sale_amount,
                        source_key,
                    ),
                )
            return
        waiting_text = (
            "⏳ <b>Waiting for the ChatGPT OTP...</b>\n\n"
            "The bot checks automatically. If no verification code arrives, the rental price "
            "is returned to your wallet. You may rent another number after 60 seconds.\n\n"
            "An OTP may arrive after 8-10 minutes. If you rent a new number and the old "
            "number later receives an OTP, both rentals are charged."
            if user.language == "en"
            else "⏳ <b>Đang chờ OTP ChatGPT...</b>\n\n"
            "Bot đang tự kiểm tra OTP. Nếu không nhận được mã, tiền thuê sẽ tự động hoàn "
            "về ví. Bạn có thể thuê số khác sau 60 giây.\n\n"
            "OTP có thể về chậm. Nếu bạn thuê số mới và số cũ sau đó vẫn nhận "
            "được mã, cả hai lượt thuê đều được tính phí."
        )
        if callback.message:
            waiting_message = await callback.message.answer(
                waiting_text,
                reply_markup=sms_waiting_menu(
                    user.language,
                    result.sale_amount,
                    source_key,
                ),
            )
            attached = await attach_sms_waiting_message(
                session_factory,
                result.rental_id or 0,
                user.telegram_id,
                waiting_message.message_id,
            )
            if not attached:
                try:
                    await waiting_message.delete()
                except TelegramBadRequest:
                    pass

    @router.callback_query(F.data == "sms:history")
    async def sms_history(callback: CallbackQuery, session: AsyncSession) -> None:
        await callback.answer()
        user = await get_or_create_user(callback, session)
        rentals = await recent_sms_rentals(session, user.telegram_id, limit=10)
        if not rentals:
            text = (
                "🧾 Bạn chưa thuê số SMS nào."
                if user.language == "vi"
                else "🧾 You have no SMS rentals yet."
            )
        else:
            status_vi = {
                "requesting": "đang lấy số",
                "pending": "đang chờ OTP",
                "success": "thành công",
                "refunded": "đã hoàn ví",
                "unknown": "cần đối soát",
            }
            lines = []
            for rental in rentals:
                status = status_vi.get(rental.status, rental.status)
                if user.language == "en":
                    status = {
                        "requesting": "requesting",
                        "pending": "waiting for OTP",
                        "success": "success",
                        "refunded": "refunded",
                        "unknown": "under review",
                    }.get(rental.status, rental.status)
                code = f" · OTP {escape(rental.otp_code)}" if rental.otp_code else ""
                country = sms_country_name(rental.provider, user.language)
                lines.append(
                    f"• <code>{escape(rental.shop_order_code or f'SMS{rental.id}')}</code> · "
                    f"{escape(country)} · <code>{escape(rental.phone_number or '—')}</code> · "
                    f"{status}{code}"
                )
            title = "🧾 <b>Lịch sử thuê số</b>" if user.language == "vi" else "🧾 <b>SMS rental history</b>"
            text = f"{title}\n\n" + "\n".join(lines)
        maintenance_enabled = await sms_rental_maintenance_enabled(session)
        availabilities = (
            await current_sms_availabilities() if not maintenance_enabled else []
        )
        if callback.message:
            await edit_or_send_text(
                callback.message,
                text,
                reply_markup=sms_rental_menu(
                    user.language,
                    sources=sms_source_buttons(availabilities),
                ),
            )

    @router.callback_query(F.data.startswith("prod:"))
    async def show_product_detail(callback: CallbackQuery, session: AsyncSession) -> None:
        user = await get_or_create_user(callback, session)
        parts = callback.data.split(":")
        product_id = int(parts[1])
        origin = parts[2] if len(parts) >= 3 and parts[2] == "quick" else None
        product = await session.get(Product, product_id)
        if product is None or not product.active:
            await callback.answer("Sản phẩm không tồn tại.", show_alert=True)
            return
        await callback.answer()
        # Background supplier workers keep this cached stock fresh. The actual
        # purchase path still refreshes the provider before charging/delivery.
        stock = await available_stock(session, product.id)
        pricing = await product_pricing(
            session,
            product,
            user_id=user.telegram_id,
        )
        display_price = pricing.final_unit_price if pricing is not None else product.price
        quantity_discounts = await active_quantity_discounts(session, product.id)
        name = sanitize_customer_text(
            product.name_en if user.language == "en" else product.name_vi
        )
        description = (
            product.description_en if user.language == "en" else product.description_vi
        )
        labels = (
            ("Price", "In stock", "Description")
            if user.language == "en"
            else ("Giá", "Còn hàng", "Thông tin")
        )
        price_label = (
            "Seller price from"
            if user.language == "en"
            and pricing is not None
            and pricing.seller_price_id is not None
            else "Giá seller từ"
            if pricing is not None and pricing.seller_price_id is not None
            else labels[0]
        )
        price_text = f"<b>{format_vnd(display_price)}</b>"
        if pricing is not None and pricing.flash_sale is not None:
            price_text = (
                f"<s>{format_vnd(product.price)}</s> → <b>{format_vnd(display_price)}</b>"
            )
        text = (
            f"{product_brand_emoji(name)} <b>{safe_customer_html(name)}</b>\n\n"
            f"📋 <b>{labels[2]}:</b>\n"
            f"{safe_customer_telegram_html(description or '—')}\n\n"
            f"💰 <b>{price_label}:</b> {price_text}\n"
            f"📦 <b>{labels[1]}:</b> <b>{stock}</b>"
        )
        if pricing is not None and pricing.flash_sale is not None:
            remaining = flash_sale_remaining(pricing.flash_sale)
            text += (
                f"\n⚡ Flash Sale còn: <b>{remaining}</b> suất"
                if user.language == "vi"
                else f"\n⚡ Flash Sale remaining: <b>{remaining}</b>"
            )
        if quantity_discounts and not (
            pricing and (pricing.flash_sale or pricing.seller_price_id is not None)
        ):
            tier_lines = "\n".join(
                f"🛒 {quantity_tier_offer_text(tier, product.price, user.language)}"
                for tier in quantity_discounts
            )
            tier_title = (
                "🎁 <b>Ưu đãi:</b>"
                if user.language == "vi"
                else "🎁 <b>Offers:</b>"
            )
            text += f"\n\n{tier_title}\n{tier_lines}"
        if callback.message:
            await callback.message.edit_text(
                text,
                reply_markup=product_detail(
                    product,
                    user.language,
                    stock,
                    allow_coupon=not bool(
                        pricing
                        and (
                            pricing.flash_sale
                            or pricing.seller_price_id is not None
                        )
                    ),
                    flash_sale_id=(
                        pricing.flash_sale.id
                        if pricing is not None and pricing.flash_sale is not None
                        else None
                    ),
                    origin=origin,
                ),
            )

    async def complete_product_purchase(
        target: Message,
        user: User,
        product_id: int,
        quantity: int,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        coupon_id: int | None = None,
        supplier_request_key: str | None = None,
        expected_flash_sale_id: int | None = None,
        expected_total_amount: int | None = None,
    ) -> str:
        fulfillment_message: Message | None = None

        async def show_fulfillment_started(_user_id: int, language: str) -> None:
            nonlocal fulfillment_message
            if fulfillment_message is not None:
                return
            fulfillment_message = await target.answer(
                "⏳ <b>Đang lấy hàng...</b>\nBạn vui lòng chờ trong giây lát."
                if language == "vi"
                else "⏳ <b>Getting your product...</b>\nPlease wait a moment."
            )

        fulfillment_source = await session.scalar(
            select(Product.fulfillment_source).where(Product.id == product_id)
        )
        # The purchase service opens its own transaction. Do not hold the
        # middleware connection while waiting for a supplier or another buyer.
        await release_session_connection(session)
        if fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES:
            await show_fulfillment_started(user.telegram_id, user.language)
        try:
            result = await purchase_product(
                session_factory,
                user.telegram_id,
                product_id,
                cipher,
                quantity,
                supplier_client,
                lehai_client=lehai_client,
                canboso_client=canboso_client,
                nce_client=nce_client,
                haji_client=haji_client,
                coupon_id=coupon_id,
                referral_commission_percent=settings.referral_commission_percent,
                on_fulfillment_started=(
                    None if fulfillment_message is not None else show_fulfillment_started
                ),
                supplier_idempotency_key=supplier_request_key,
                expected_flash_sale_id=expected_flash_sale_id,
                expected_total_amount=expected_total_amount,
            )
        finally:
            if fulfillment_message is not None:
                try:
                    await fulfillment_message.delete()
                except Exception:
                    pass
        messages_vi = {
            "out_of_stock": "Sản phẩm vừa hết hàng.",
            "blocked": "Tài khoản đang bị khóa. Liên hệ hỗ trợ.",
            "not_found": "Sản phẩm không tồn tại.",
            "invalid_quantity": "Số lượng không hợp lệ.",
            "supplier_unavailable": "Nguồn hàng đang tạm gián đoạn. Vui lòng thử lại sau.",
            "invalid_coupon": "Mã giảm giá không hợp lệ, đã hết hạn hoặc hết lượt sử dụng.",
            "flash_sale_unavailable": (
                "Suất Flash Sale vừa hết hoặc giá vốn đã tăng. Bạn chưa bị trừ tiền."
            ),
            "price_changed": "Giá vừa thay đổi. Vui lòng xem lại tổng tiền trước khi xác nhận.",
        }
        messages_en = {
            "out_of_stock": "This product is out of stock.",
            "blocked": "Your account is blocked. Please contact support.",
            "not_found": "Product not found.",
            "invalid_quantity": "Invalid quantity.",
            "supplier_unavailable": "The supplier is temporarily unavailable. Please try again.",
            "invalid_coupon": "This discount code is invalid, expired, or fully used.",
            "flash_sale_unavailable": (
                "The Flash Sale allocation ended or supplier cost increased. You were not charged."
            ),
            "price_changed": "The price changed. Please review the total before confirming again.",
        }
        if not result.ok:
            if result.message == "insufficient":
                product = await session.get(Product, product_id)
                if product is not None:
                    total_amount = result.total_amount or product.price * quantity
                    coupon_line_vi = (
                        f"🏷️ Mã giảm giá: <b>{escape(result.coupon_code)}</b> "
                        f"(tổng ưu đãi {format_vnd(result.discount_amount)})\n"
                        if result.coupon_code
                        else ""
                    )
                    coupon_line_en = (
                        f"🏷️ Discount code: <b>{escape(result.coupon_code)}</b> "
                        f"(total savings {format_vnd(result.discount_amount)})\n"
                        if result.coupon_code
                        else ""
                    )
                    quantity_line_vi = (
                        "🎁 Ưu đãi số lượng: "
                        f"{applied_quantity_discount_text(result.quantity_discount_type, result.quantity_discount_value, 'vi')}\n"
                        if result.quantity_discount_type
                        else ""
                    )
                    quantity_line_en = (
                        "🎁 Quantity discount: "
                        f"{applied_quantity_discount_text(result.quantity_discount_type, result.quantity_discount_value, 'en')}\n"
                        if result.quantity_discount_type
                        else ""
                    )
                    text = (
                        "💳 <b>Số dư chưa đủ</b>\n\n"
                        f"📦 Sản phẩm: {product_brand_emoji(product.name_vi)} "
                        f"<b>{safe_customer_html(product.name_vi)}</b>\n"
                        f"🧮 Số lượng: <b>{quantity}</b>\n"
                        f"{coupon_line_vi}"
                        f"{quantity_line_vi}"
                        f"💰 Tổng tiền: <b>{format_vnd(total_amount)}</b>\n"
                        f"👛 Số dư hiện có: <b>{format_vnd(user.balance)}</b>\n\n"
                        "Bạn có thể thanh toán QR trực tiếp cho sản phẩm này. "
                        "Số dư hiện có vẫn được giữ nguyên."
                        if user.language == "vi"
                        else "💳 <b>Insufficient balance</b>\n\n"
                        f"📦 Product: {product_brand_emoji(product.name_en)} "
                        f"<b>{safe_customer_html(product.name_en)}</b>\n"
                        f"🧮 Quantity: <b>{quantity}</b>\n"
                        f"{coupon_line_en}"
                        f"{quantity_line_en}"
                        f"💰 Total: <b>{format_vnd(total_amount)}</b>\n"
                        f"👛 Current balance: <b>{format_vnd(user.balance)}</b>\n\n"
                        "You can pay for this product directly by QR. "
                        "Your current balance remains unchanged."
                    )
                    await target.answer(
                        text,
                        reply_markup=purchase_payment_options(
                            product.id,
                            quantity,
                            user.language,
                            coupon_id,
                            result.flash_sale_id,
                        ),
                    )
                return result.message
            if result.message.startswith("coupon_"):
                await target.answer(coupon_error_message(result.message, user.language))
                return result.message
            labels = messages_en if user.language == "en" else messages_vi
            await target.answer(labels.get(result.message, "Error"))
            return result.message
        if result.orders and result.secrets:
            product_name = sanitize_customer_text(
                result.orders[0].display_name_en
                if user.language == "en"
                else result.orders[0].display_name_vi
            )
            order_ids = [order.id for order in result.orders]
            text = delivery_text(
                shop_order_code=result.orders[0].shop_order_code,
                product_name=product_name,
                secrets=result.secrets,
                total_amount=result.total_amount,
                language=user.language,
            )
            if result.discount_amount:
                has_quantity_discount = result.quantity_discount_type is not None
                quantity_label_vi = applied_quantity_discount_text(
                    result.quantity_discount_type,
                    result.quantity_discount_value,
                    "vi",
                )
                quantity_label_en = applied_quantity_discount_text(
                    result.quantity_discount_type,
                    result.quantity_discount_value,
                    "en",
                )
                discount_label = (
                    f"Mã <b>{escape(result.coupon_code)}</b> và ưu đãi số lượng"
                    if result.coupon_code and has_quantity_discount
                    else f"Mã <b>{escape(result.coupon_code)}</b>"
                    if result.coupon_code
                    else f"Ưu đãi số lượng {quantity_label_vi}"
                )
                discount_label_en = (
                    f"Code <b>{escape(result.coupon_code)}</b> and quantity discount"
                    if result.coupon_code and has_quantity_discount
                    else f"Code <b>{escape(result.coupon_code)}</b>"
                    if result.coupon_code
                    else f"Quantity discount {quantity_label_en}"
                )
                text += (
                    f"\n\n🏷️ {discount_label} đã giảm tổng "
                    f"<b>{format_vnd(result.discount_amount)}</b>."
                    if user.language == "vi"
                    else f"\n\n🏷️ {discount_label_en} saved "
                    f"<b>{format_vnd(result.discount_amount)}</b>."
                )
            await target.answer(
                text,
                reply_markup=delivery_keyboard(
                    primary_order_id=min(order_ids),
                    secrets=result.secrets,
                    language=user.language,
                ),
            )
            await send_purchase_tutorials(
                target.bot,
                user.telegram_id,
                result.orders[0].product.supplier_product_id,
                user.language,
                session_factory,
            )
        return result.message

    async def show_seller_purchase_confirmation(
        target: Message,
        user: User,
        product: Product,
        quantity: int,
        session: AsyncSession,
        *,
        expected_flash_sale_id: int | None = None,
    ) -> bool:
        pricing = await product_pricing(
            session,
            product,
            quantity=quantity,
            user_id=user.telegram_id,
            expected_flash_sale_id=expected_flash_sale_id,
        )
        if pricing is None or pricing.seller_price_id is None:
            return False
        quote = await product_checkout_quote(
            session,
            product,
            quantity,
            pricing,
            supplier_client,
            lehai_client,
            canboso_client=canboso_client,
            nce_client=nce_client,
            haji_client=haji_client,
        )
        if not quote.available:
            await target.answer(
                "Nguồn hàng vừa thay đổi, vui lòng thử lại."
                if user.language == "vi"
                else "Stock just changed. Please try again."
            )
            return True
        name = sanitize_customer_text(
            product.name_en if user.language == "en" else product.name_vi
        )
        breakdown = unit_price_breakdown_text(quote.unit_prices)
        pricing_note_vi = (
            "Giá seller được tính theo giá vốn từng tài khoản. Bot chỉ trừ đúng tổng tiền trên."
            if quote.pricing.seller_price_id is not None
            else "Có tài khoản không đủ biên lợi nhuận seller nên đơn này dùng giá bán lẻ. Bot chỉ trừ đúng tổng tiền trên."
        )
        pricing_note_en = (
            "Seller pricing follows the cost of each account. Only this exact total will be charged."
            if quote.pricing.seller_price_id is not None
            else "At least one account cannot safely use seller pricing, so this order uses retail pricing. Only this exact total will be charged."
        )
        text = (
            "🧾 <b>Xác nhận mua hàng</b>\n\n"
            f"📦 Sản phẩm: {product_brand_emoji(name)} <b>{safe_customer_html(name)}</b>\n"
            f"🧮 Số lượng: <b>{quantity}</b>\n"
            f"💰 Chi tiết giá: {breakdown}\n"
            f"💳 Tổng trừ ví: <b>{format_vnd(quote.total_amount)}</b>\n\n"
            f"{pricing_note_vi}"
            if user.language == "vi"
            else "🧾 <b>Confirm purchase</b>\n\n"
            f"📦 Product: {product_brand_emoji(name)} <b>{safe_customer_html(name)}</b>\n"
            f"🧮 Quantity: <b>{quantity}</b>\n"
            f"💰 Price breakdown: {breakdown}\n"
            f"💳 Wallet total: <b>{format_vnd(quote.total_amount)}</b>\n\n"
            f"{pricing_note_en}"
        )
        await target.answer(
            text,
            reply_markup=seller_purchase_confirmation_menu(
                product.id,
                quantity,
                quote.total_amount,
                user.language,
                expected_flash_sale_id,
            ),
        )
        return True

    @router.callback_query(F.data.startswith("coupon:"))
    async def request_discount_code(
        callback: CallbackQuery,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        user = await get_or_create_user(callback, session)
        product_id = int(callback.data.split(":", 1)[1])
        product = await session.get(Product, product_id)
        if product is None or not product.active:
            await callback.answer("Sản phẩm không tồn tại.", show_alert=True)
            return
        pricing = await product_pricing(
            session,
            product,
            user_id=user.telegram_id,
        )
        if pricing is not None and pricing.flash_sale is not None:
            await callback.answer(
                "Sản phẩm đang Flash Sale nên không cộng thêm mã giảm giá.",
                show_alert=True,
            )
            return
        if pricing is not None and pricing.seller_price_id is not None:
            await callback.answer(
                "Giá seller đã là giá riêng theo giá vốn nên không cộng thêm mã giảm giá."
                if user.language == "vi"
                else "Seller pricing already follows source cost and cannot be stacked with coupons.",
                show_alert=True,
            )
            return
        await state.set_state(PurchaseStates.waiting_for_coupon)
        await state.update_data(product_id=product.id)
        prompt = (
            f"🏷 <b>Nhập mã giảm giá</b>\n\nSản phẩm: "
            f"{product_brand_emoji(product.name_vi)} <b>{safe_customer_html(product.name_vi)}</b>\n"
            "Gửi mã giảm giá bạn muốn sử dụng."
            if user.language == "vi"
            else f"🏷 <b>Apply a discount code</b>\n\nProduct: "
            f"{product_brand_emoji(product.name_en)} "
            f"<b>{safe_customer_html(product.name_en)}</b>\nSend the code you want to use."
        )
        if callback.message:
            await callback.message.edit_text(prompt, reply_markup=back_menu(user.language))
        await callback.answer()

    @router.message(PurchaseStates.waiting_for_coupon)
    async def receive_discount_code(
        message: Message,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        user = await get_or_create_user(message, session)
        data = await state.get_data()
        product = await session.get(Product, int(data.get("product_id", 0)))
        if product is None or not product.active:
            await state.clear()
            await message.answer("Sản phẩm không còn tồn tại.")
            return
        stock = await available_stock(session, product.id)
        try:
            pricing = await product_pricing(
                session,
                product,
                coupon_code=message.text or "",
                user_id=user.telegram_id,
                raise_coupon_error=True,
            )
        except CouponValidationError as exc:
            await message.answer(
                coupon_error_message(exc.code, user.language)
            )
            return
        if stock <= 0:
            await state.clear()
            await message.answer(
                "Sản phẩm vừa hết hàng."
                if user.language == "vi"
                else "This product is now out of stock."
            )
            return
        await state.clear()
        coupon = pricing.coupon
        if coupon is None:
            if pricing.flash_sale is not None:
                await message.answer(
                    "Sản phẩm đang Flash Sale nên không áp dụng thêm mã giảm giá."
                    if user.language == "vi"
                    else "This product is on Flash Sale, so coupon stacking is disabled."
                )
            return
        text = (
            f"✅ <b>Đã áp dụng mã {escape(coupon.code)}</b>\n\n"
            f"💰 Giá gốc: <s>{format_vnd(pricing.original_unit_price)}</s>\n"
            f"🏷️ Giảm mỗi sản phẩm: <b>{format_vnd(pricing.discount_per_unit)}</b>\n"
            f"✅ Giá còn lại: <b>{format_vnd(pricing.final_unit_price)}</b>\n\n"
            "Chọn số lượng để mua với mức giá này."
            if user.language == "vi"
            else f"✅ <b>Code {escape(coupon.code)} applied</b>\n\n"
            f"💰 Original: <s>{format_vnd(pricing.original_unit_price)}</s>\n"
            f"🏷️ Discount per item: <b>{format_vnd(pricing.discount_per_unit)}</b>\n"
            f"✅ Final price: <b>{format_vnd(pricing.final_unit_price)}</b>\n\n"
            "Choose a quantity to continue."
        )
        await message.answer(
            text,
            reply_markup=coupon_quantity_menu(
                product,
                user.language,
                stock,
                coupon.id,
                pricing.final_unit_price,
            ),
        )

    @router.callback_query(F.data.startswith("qtymenu:"))
    async def choose_purchase_quantity(callback: CallbackQuery, session: AsyncSession) -> None:
        user = await get_or_create_user(callback, session)
        parts = callback.data.split(":")
        product_id = int(parts[1])
        expected_flash_sale_id = (
            int(parts[3]) if len(parts) >= 4 and parts[2] == "flash" else None
        )
        origin = (
            parts[parts.index("origin") + 1]
            if "origin" in parts and parts.index("origin") + 1 < len(parts)
            else None
        )
        product = await session.get(Product, product_id)
        if product is None or not product.active:
            await callback.answer("Sản phẩm không tồn tại.", show_alert=True)
            return
        stock = await available_stock(session, product.id)
        pricing = await product_pricing(
            session,
            product,
            user_id=user.telegram_id,
            expected_flash_sale_id=expected_flash_sale_id,
        )
        if pricing is None and expected_flash_sale_id is not None:
            await callback.answer(
                "Suất Flash Sale vừa hết hoặc giá vốn đã tăng.",
                show_alert=True,
            )
            return
        display_price = pricing.final_unit_price if pricing is not None else product.price
        menu_stock = stock
        if pricing is not None and pricing.flash_sale is not None:
            menu_stock = min(stock, flash_sale_remaining(pricing.flash_sale))
        maximum = purchase_quantity_limit(product, menu_stock)
        quantity_discounts = await active_quantity_discounts(session, product.id)
        if maximum <= 0:
            if callback.message:
                await callback.message.edit_reply_markup(
                    reply_markup=product_detail(
                        product,
                        user.language,
                        0,
                        origin=origin,
                    )
                )
            await callback.answer("Sản phẩm đã hết hàng.", show_alert=True)
            return
        text = (
            f"🧮 <b>Chọn số lượng</b>\n\n"
            f"📦 Sản phẩm: {product_brand_emoji(product.name_vi)} "
            f"<b>{safe_customer_html(product.name_vi)}</b>\n"
            f"💰 {'Giá seller từ' if pricing is not None and pricing.seller_price_id is not None else 'Đơn giá'}: <b>{format_vnd(display_price)}</b>\n"
            f"📦 Còn hàng: <b>{stock}</b>\n"
            f"🧾 Tối đa mỗi lần: <b>{maximum}</b>"
            if user.language == "vi"
            else f"🧮 <b>Choose quantity</b>\n\n"
            f"📦 Product: {product_brand_emoji(product.name_en)} "
            f"<b>{safe_customer_html(product.name_en)}</b>\n"
            f"💰 {'Seller price from' if pricing is not None and pricing.seller_price_id is not None else 'Unit price'}: <b>{format_vnd(display_price)}</b>\n"
            f"📦 In stock: <b>{stock}</b>\n"
            f"🧾 Maximum per order: <b>{maximum}</b>"
        )
        if quantity_discounts and not (
            pricing and (pricing.flash_sale or pricing.seller_price_id is not None)
        ):
            tier_summary = "\n".join(
                f"🎁 {quantity_tier_offer_text(tier, product.price, user.language)}"
                for tier in quantity_discounts
            )
            text += f"\n\n{tier_summary}"
        if callback.message:
            await callback.message.edit_text(
                text,
                reply_markup=quantity_menu(
                    product,
                    user.language,
                    menu_stock,
                    display_price,
                    expected_flash_sale_id,
                    origin,
                    variable_price=bool(
                        pricing is not None and pricing.seller_price_id is not None
                    ),
                ),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("customqty:"))
    async def custom_purchase_quantity(
        callback: CallbackQuery,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        user = await get_or_create_user(callback, session)
        parts = callback.data.split(":")
        product_id = int(parts[1])
        expected_flash_sale_id = (
            int(parts[3]) if len(parts) >= 4 and parts[2] == "flash" else None
        )
        product = await session.get(Product, product_id)
        if product is None or not product.active or not product.allow_quantity:
            await callback.answer("Sản phẩm không hợp lệ.", show_alert=True)
            return
        stock = await available_stock(session, product.id)
        if stock <= 0:
            await callback.answer("Sản phẩm đã hết hàng.", show_alert=True)
            return
        pricing = await product_pricing(
            session,
            product,
            user_id=user.telegram_id,
            expected_flash_sale_id=expected_flash_sale_id,
        )
        if pricing is None and expected_flash_sale_id is not None:
            await callback.answer(
                "Suất Flash Sale vừa hết hoặc giá vốn đã tăng.",
                show_alert=True,
            )
            return
        flash_limit = (
            flash_sale_remaining(pricing.flash_sale)
            if pricing is not None and pricing.flash_sale is not None
            else stock
        )
        maximum = min(purchase_quantity_limit(product, stock), flash_limit)
        await state.set_state(PurchaseStates.waiting_for_quantity)
        await state.update_data(
            product_id=product.id,
            maximum_quantity=maximum,
            expected_flash_sale_id=expected_flash_sale_id,
        )
        prompt = (
            f"Nhập số lượng từ 1 đến {maximum}."
            if user.language == "vi"
            else f"Enter a quantity from 1 to {maximum}."
        )
        if callback.message:
            await callback.message.edit_text(prompt, reply_markup=back_menu(user.language))
        await callback.answer()

    @router.message(PurchaseStates.waiting_for_quantity)
    async def receive_purchase_quantity(
        message: Message,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        state: FSMContext,
    ) -> None:
        user = await get_or_create_user(message, session)
        data = await state.get_data()
        product_id = int(data.get("product_id", 0))
        maximum_quantity = int(data.get("maximum_quantity", 0))
        expected_flash_sale_id = data.get("expected_flash_sale_id")
        product = await session.get(Product, product_id)
        try:
            quantity = int((message.text or "").strip())
        except ValueError:
            quantity = 0
        maximum = min(product.max_quantity, maximum_quantity) if product is not None else 1
        if product is None or quantity < 1 or quantity > maximum:
            await message.answer(f"Số lượng không hợp lệ. Hãy nhập từ 1 đến {maximum}.")
            return
        await state.clear()
        normalized_flash_sale_id = (
            int(expected_flash_sale_id)
            if expected_flash_sale_id is not None
            else None
        )
        if await show_seller_purchase_confirmation(
            message,
            user,
            product,
            quantity,
            session,
            expected_flash_sale_id=normalized_flash_sale_id,
        ):
            return
        await complete_product_purchase(
            message,
            user,
            product.id,
            quantity,
            session,
            session_factory,
            supplier_request_key=f"tg-message-{message.chat.id}-{message.message_id}",
            expected_flash_sale_id=normalized_flash_sale_id,
        )

    @router.callback_query(F.data.startswith("customcouponqty:"))
    async def custom_coupon_quantity(
        callback: CallbackQuery,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        user = await get_or_create_user(callback, session)
        _, product_id_text, coupon_id_text = callback.data.split(":")
        product = await session.get(Product, int(product_id_text))
        if product is None or not product.active or not product.allow_quantity:
            await callback.answer("Sản phẩm không hợp lệ.", show_alert=True)
            return
        try:
            pricing = await product_pricing(
                session,
                product,
                coupon_id=int(coupon_id_text),
                user_id=user.telegram_id,
                raise_coupon_error=True,
            )
        except CouponValidationError as exc:
            await callback.answer(
                coupon_error_message(exc.code, user.language),
                show_alert=True,
            )
            return
        stock = await available_stock(session, product.id)
        maximum = purchase_quantity_limit(product, stock)
        if maximum <= 0:
            await callback.answer("Sản phẩm đã hết hàng.", show_alert=True)
            return
        await state.set_state(PurchaseStates.waiting_for_coupon_quantity)
        await state.update_data(
            product_id=product.id,
            coupon_id=pricing.coupon.id if pricing.coupon else 0,
            maximum_quantity=maximum,
        )
        prompt = (
            f"Nhập số lượng từ 1 đến {maximum}. Giá sau giảm mỗi sản phẩm: "
            f"{format_vnd(pricing.final_unit_price)}."
            if user.language == "vi"
            else f"Enter a quantity from 1 to {maximum}. Discounted unit price: "
            f"{format_vnd(pricing.final_unit_price)}."
        )
        if callback.message:
            await callback.message.edit_text(prompt, reply_markup=back_menu(user.language))
        await callback.answer()

    @router.message(PurchaseStates.waiting_for_coupon_quantity)
    async def receive_coupon_quantity(
        message: Message,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        state: FSMContext,
    ) -> None:
        user = await get_or_create_user(message, session)
        data = await state.get_data()
        product_id = int(data.get("product_id", 0))
        coupon_id = int(data.get("coupon_id", 0))
        maximum = int(data.get("maximum_quantity", 0))
        try:
            quantity = int((message.text or "").strip())
        except ValueError:
            quantity = 0
        if quantity < 1 or quantity > maximum:
            await message.answer(f"Số lượng không hợp lệ. Hãy nhập từ 1 đến {maximum}.")
            return
        await state.clear()
        await complete_product_purchase(
            message,
            user,
            product_id,
            quantity,
            session,
            session_factory,
            coupon_id,
            supplier_request_key=f"tg-message-{message.chat.id}-{message.message_id}",
        )

    @router.callback_query(F.data.startswith("buy:"))
    async def buy_product(
        callback: CallbackQuery,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await get_or_create_user(callback, session)
        parts = callback.data.split(":")
        product_id = int(parts[1])
        quantity = int(parts[2]) if len(parts) > 2 else 1
        expected_flash_sale_id = (
            int(parts[4])
            if len(parts) >= 5 and parts[3] == "flash"
            else None
        )
        product = await session.get(Product, product_id)
        if product is None or not product.active:
            await callback.answer("Sản phẩm không tồn tại.", show_alert=True)
            return
        if callback.message and await show_seller_purchase_confirmation(
            callback.message,
            user,
            product,
            quantity,
            session,
            expected_flash_sale_id=expected_flash_sale_id,
        ):
            await callback.answer()
            return
        await callback.answer(
            "Đang xử lý đơn hàng..."
            if user.language == "vi"
            else "Processing your order..."
        )
        if callback.message:
            await complete_product_purchase(
                callback.message,
                user,
                product_id,
                quantity,
                session,
                session_factory,
                supplier_request_key=f"tg-callback-{callback.id}",
                expected_flash_sale_id=expected_flash_sale_id,
            )

    @router.callback_query(F.data.startswith("sellerbuy:"))
    async def confirm_seller_purchase(
        callback: CallbackQuery,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await get_or_create_user(callback, session)
        parts = callback.data.split(":")
        product_id = int(parts[1])
        quantity = int(parts[2])
        expected_total_amount = int(parts[3])
        expected_flash_sale_id = (
            int(parts[5])
            if len(parts) >= 6 and parts[4] == "flash"
            else None
        )
        await callback.answer(
            "Đang xử lý đơn hàng..."
            if user.language == "vi"
            else "Processing your order..."
        )
        if callback.message:
            await complete_product_purchase(
                callback.message,
                user,
                product_id,
                quantity,
                session,
                session_factory,
                supplier_request_key=f"tg-callback-{callback.id}",
                expected_flash_sale_id=expected_flash_sale_id,
                expected_total_amount=expected_total_amount,
            )

    @router.callback_query(F.data.startswith("buycoupon:"))
    async def buy_product_with_coupon(
        callback: CallbackQuery,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await get_or_create_user(callback, session)
        _, product_id_text, quantity_text, coupon_id_text = callback.data.split(":")
        await callback.answer(
            "Đang xử lý đơn hàng..."
            if user.language == "vi"
            else "Processing your order..."
        )
        if callback.message:
            await complete_product_purchase(
                callback.message,
                user,
                int(product_id_text),
                int(quantity_text),
                session,
                session_factory,
                int(coupon_id_text),
                supplier_request_key=f"tg-callback-{callback.id}",
            )

    @router.callback_query(F.data.startswith("directpay:"))
    async def direct_product_payment(
        callback: CallbackQuery,
        session: AsyncSession,
    ) -> None:
        user = await get_or_create_user(callback, session)
        if not settings.sepay_enabled:
            await callback.answer("Thanh toán QR chưa được bật.", show_alert=True)
            return
        parts = callback.data.split(":")
        product_id = int(parts[1])
        quantity = int(parts[2]) if len(parts) > 2 else 1
        coupon_id = None
        expected_flash_sale_id = None
        if len(parts) > 3:
            if parts[3] == "flash" and len(parts) > 4:
                expected_flash_sale_id = int(parts[4])
            else:
                coupon_id = int(parts[3])
        product = await session.get(Product, product_id)
        if product is None or not product.active:
            await callback.answer("Sản phẩm không tồn tại.", show_alert=True)
            return
        if quantity < 1 or quantity > product.max_quantity:
            await callback.answer("Số lượng không hợp lệ.", show_alert=True)
            return
        if quantity > 1 and not product.allow_quantity:
            await callback.answer("Sản phẩm không hỗ trợ mua nhiều.", show_alert=True)
            return
        local_stock = await local_inventory_stock(session, product.id)
        if local_stock < quantity and (
            await available_stock(session, product.id)
            < quantity
        ):
            await callback.answer("Sản phẩm vừa hết hàng.", show_alert=True)
            return

        try:
            pricing = await product_pricing(
                session,
                product,
                coupon_id=coupon_id,
                quantity=quantity,
                user_id=user.telegram_id,
                expected_flash_sale_id=expected_flash_sale_id,
                raise_coupon_error=True,
            )
        except CouponValidationError as exc:
            await callback.answer(
                coupon_error_message(exc.code, user.language),
                show_alert=True,
            )
            return
        if pricing is None:
            if expected_flash_sale_id is not None:
                await callback.answer(
                    "Suất Flash Sale vừa hết hoặc giá vốn đã tăng.",
                    show_alert=True,
                )
                return
            await callback.answer("Mã giảm giá không còn hiệu lực.", show_alert=True)
            return
        checkout_quote = await product_checkout_quote(
            session,
            product,
            quantity,
            pricing,
            supplier_client,
            lehai_client,
            canboso_client=canboso_client,
            nce_client=nce_client,
            haji_client=haji_client,
        )
        if not checkout_quote.available:
            await callback.answer(
                "Nguồn hàng vừa thay đổi, vui lòng thử lại.",
                show_alert=True,
            )
            return
        pricing = checkout_quote.pricing
        supplier_quote = checkout_quote.supplier_quote
        if (
            supplier_quote is not None
            and pricing.flash_sale is not None
            and any(
                allocation.route.snapshot.unit_price
                > allocation.final_unit_price
                for allocation in supplier_quote.allocations
            )
        ):
            await callback.answer(
                "Giá vốn phần hàng còn lại cao hơn giá Flash Sale.",
                show_alert=True,
            )
            return
        total_amount = checkout_quote.total_amount
        total_discount = checkout_quote.discount_amount
        try:
            deposit = await create_deposit(
                session,
                user.telegram_id,
                total_amount,
                settings.payment_prefix,
                payment_kind="direct_purchase",
                product_id=product.id,
                quantity=quantity,
                discount_amount=total_discount,
                discount_code_id=pricing.coupon.id if pricing.coupon else None,
                discount_code=pricing.coupon.code if pricing.coupon else None,
                flash_sale_id=pricing.flash_sale.id if pricing.flash_sale else None,
                flash_sale_quantity=quantity if pricing.flash_sale else 0,
                seller_price_id=pricing.seller_price_id,
                seller_profit_per_unit=pricing.seller_profit_per_unit,
                expiry_seconds=settings.payment_expiry_seconds,
                max_pending_deposits=settings.max_pending_deposits_per_user,
            )
        except PendingDepositLimitReached:
            await callback.answer(
                "Bạn đang có quá nhiều QR chờ thanh toán. Hãy dùng QR cũ hoặc chờ hết hạn.",
                show_alert=True,
            )
            return
        except FlashSaleUnavailable:
            await session.rollback()
            await callback.answer(
                "Suất Flash Sale vừa hết. Vui lòng mở lại sản phẩm để xem giá hiện tại.",
                show_alert=True,
            )
            return
        except ValueError:
            await session.rollback()
            await callback.answer(
                "Giá seller vừa thay đổi. Vui lòng mở lại sản phẩm để lấy giá mới."
                if user.language == "vi"
                else "Seller pricing just changed. Please reopen the product.",
                show_alert=True,
            )
            return
        qr_url = build_sepay_qr_url(
            settings.bank_code,
            settings.bank_account,
            total_amount,
            deposit.code,
        )
        product_name = sanitize_customer_text(
            product.name_en if user.language == "en" else product.name_vi
        )
        grouped_prices = unit_price_breakdown(checkout_quote.unit_prices)
        price_breakdown_vi = (
            "💰 Chi tiết giá: "
            + " + ".join(
                f"<b>{price_quantity}</b> × <b>{format_vnd(unit_price)}</b>"
                for price_quantity, unit_price in grouped_prices
            )
            + "\n"
            if len(grouped_prices) > 1
            else ""
        )
        price_breakdown_en = (
            "💰 Price breakdown: "
            + " + ".join(
                f"<b>{price_quantity}</b> × <b>{format_vnd(unit_price)}</b>"
                for price_quantity, unit_price in grouped_prices
            )
            + "\n"
            if len(grouped_prices) > 1
            else ""
        )
        coupon_discount_amount = checkout_quote.coupon_discount_amount
        quantity_discount_amount = checkout_quote.quantity_discount_amount
        coupon_line_vi = (
            f"🏷️ Mã giảm giá: <b>{escape(pricing.coupon.code)}</b> "
            f"(-{format_vnd(coupon_discount_amount)})\n"
            if pricing.coupon
            else ""
        )
        coupon_line_en = (
            f"🏷️ Discount code: <b>{escape(pricing.coupon.code)}</b> "
            f"(-{format_vnd(coupon_discount_amount)})\n"
            if pricing.coupon
            else ""
        )
        quantity_line_vi = (
            "🎁 Ưu đãi số lượng: "
            f"{applied_quantity_discount_text(pricing.quantity_discount_type, pricing.quantity_discount_value, 'vi')} "
            f"(-{format_vnd(quantity_discount_amount)})\n"
            if pricing.quantity_discount_type
            else ""
        )
        quantity_line_en = (
            "🎁 Quantity discount: "
            f"{applied_quantity_discount_text(pricing.quantity_discount_type, pricing.quantity_discount_value, 'en')} "
            f"(-{format_vnd(quantity_discount_amount)})\n"
            if pricing.quantity_discount_type
            else ""
        )
        text = (
            "🧾 <b>Thanh toán sản phẩm</b>\n\n"
            f"📦 Sản phẩm: {product_brand_emoji(product_name)} "
            f"<b>{safe_customer_html(product_name)}</b>\n"
            f"🧮 Số lượng: <b>{quantity}</b>\n"
            f"{price_breakdown_vi}"
            f"{coupon_line_vi}"
            f"{quantity_line_vi}"
            f"🏦 Ngân hàng: <b>{escape(settings.bank_code)}</b>\n"
            f"💳 Số tài khoản: <code>{escape(settings.bank_account)}</code>\n"
            f"👤 Chủ tài khoản: <b>{escape(settings.bank_account_name)}</b>\n"
            f"💰 Số tiền: <b>{format_vnd(total_amount)}</b>\n"
            f"🧾 Nội dung bắt buộc: <code>{deposit.code}</code>\n\n"
            "Giữ nguyên số tiền và nội dung. Sản phẩm sẽ được giao tự động sau khi "
            "giao dịch được ghi nhận."
            "\n\n⏳ QR chỉ có hiệu lực 5 phút. Quá hạn bot sẽ xóa tin nhắn và giao dịch thất bại."
            if user.language == "vi"
            else "🧾 <b>Product payment</b>\n\n"
            f"📦 Product: {product_brand_emoji(product_name)} "
            f"<b>{safe_customer_html(product_name)}</b>\n"
            f"🧮 Quantity: <b>{quantity}</b>\n"
            f"{price_breakdown_en}"
            f"{coupon_line_en}"
            f"{quantity_line_en}"
            f"🏦 Bank: <b>{escape(settings.bank_code)}</b>\n"
            f"💳 Account: <code>{escape(settings.bank_account)}</code>\n"
            f"👤 Account name: <b>{escape(settings.bank_account_name)}</b>\n"
            f"💰 Amount: <b>{format_vnd(total_amount)}</b>\n"
            f"🧾 Required content: <code>{deposit.code}</code>\n\n"
            "Keep the exact amount and content. The product is delivered automatically "
            "after the payment is recorded."
            "\n\n⏳ This QR is valid for 5 minutes. After that, the message is deleted and "
            "the payment request fails."
        )
        if callback.message:
            try:
                sent = await callback.message.answer_photo(
                    qr_url,
                    caption=text,
                    reply_markup=back_menu(user.language),
                )
            except TelegramBadRequest:
                sent = await callback.message.answer(
                    f'{text}\n\n<a href="{qr_url}">Mở mã QR / Open QR</a>',
                    reply_markup=back_menu(user.language),
                    disable_web_page_preview=True,
                )
            await register_deposit_message(
                session,
                deposit.id,
                sent.chat.id,
                sent.message_id,
            )
        await callback.answer()

    @router.callback_query(F.data == "menu:deposit")
    async def deposit_menu(callback: CallbackQuery, session: AsyncSession) -> None:
        user = await get_or_create_user(callback, session)
        if not settings.sepay_enabled:
            text = (
                "💳 Chức năng nạp tiền đang được cấu hình. Vui lòng quay lại sau."
                if user.language == "vi"
                else "💳 Deposits are being configured. Please check again later."
            )
            if callback.message:
                await callback.message.edit_text(text, reply_markup=back_menu(user.language))
            await callback.answer()
            return
        text = (
            f"💳 <b>Nạp tiền tự động</b>\n\n"
            f"Chọn số tiền muốn nạp. Tối thiểu {format_vnd(settings.min_deposit)}."
            if user.language == "vi"
            else f"💳 <b>Automatic deposit</b>\n\n"
            f"Choose an amount. Minimum {format_vnd(settings.min_deposit)}."
        )
        if callback.message:
            await callback.message.edit_text(text, reply_markup=deposit_amounts(user.language))
        await callback.answer()

    @router.callback_query(F.data.startswith("deposit:"))
    async def choose_deposit_amount(
        callback: CallbackQuery,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        user = await get_or_create_user(callback, session)
        if not settings.sepay_enabled:
            await callback.answer("Nạp tiền chưa được bật.", show_alert=True)
            return
        raw_amount = callback.data.split(":", 1)[1]
        if raw_amount == "other":
            await state.set_state(DepositStates.waiting_for_amount)
            prompt = (
                f"Nhập số tiền muốn nạp, tối thiểu {format_vnd(settings.min_deposit)}."
                if user.language == "vi"
                else f"Enter an amount, minimum {format_vnd(settings.min_deposit)}."
            )
            if callback.message:
                await callback.message.edit_text(prompt, reply_markup=back_menu(user.language))
            await callback.answer()
            return
        await create_and_show_deposit(callback.message, session, user, int(raw_amount))
        await callback.answer()

    @router.message(DepositStates.waiting_for_amount)
    async def receive_deposit_amount(
        message: Message,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        user = await get_or_create_user(message, session)
        amount = parse_vnd(message.text or "")
        if amount is None or amount < settings.min_deposit:
            await message.answer(
                f"Số tiền không hợp lệ. Tối thiểu {format_vnd(settings.min_deposit)}."
                if user.language == "vi"
                else f"Invalid amount. Minimum {format_vnd(settings.min_deposit)}."
            )
            return
        await state.clear()
        await create_and_show_deposit(message, session, user, amount)

    async def create_and_show_deposit(
        target: Message | None,
        session: AsyncSession,
        user: User,
        amount: int,
    ) -> None:
        if target is None:
            return
        if amount < settings.min_deposit:
            await target.answer(f"Số tiền tối thiểu là {format_vnd(settings.min_deposit)}.")
            return
        try:
            deposit = await create_deposit(
                session,
                user.telegram_id,
                amount,
                settings.payment_prefix,
                expiry_seconds=settings.payment_expiry_seconds,
                max_pending_deposits=settings.max_pending_deposits_per_user,
            )
        except PendingDepositLimitReached:
            await target.answer(
                "Bạn đang có quá nhiều QR chờ thanh toán. Hãy dùng QR cũ hoặc chờ tối đa 5 phút."
            )
            return
        qr_url = build_sepay_qr_url(settings.bank_code, settings.bank_account, amount, deposit.code)
        text = (
            "💳 <b>Thông tin chuyển khoản</b>\n\n"
            f"🏦 Ngân hàng: <b>{escape(settings.bank_code)}</b>\n"
            f"💳 Số tài khoản: <code>{escape(settings.bank_account)}</code>\n"
            f"👤 Chủ tài khoản: <b>{escape(settings.bank_account_name)}</b>\n"
            f"💰 Số tiền: <b>{format_vnd(amount)}</b>\n"
            f"🧾 Nội dung bắt buộc: <code>{deposit.code}</code>\n\n"
            "Giữ nguyên số tiền và nội dung. Số dư sẽ được cập nhật tự động."
            "\n\n⏳ QR chỉ có hiệu lực 5 phút. Quá hạn bot sẽ xóa tin nhắn và giao dịch thất bại."
            if user.language == "vi"
            else "💳 <b>Bank transfer details</b>\n\n"
            f"🏦 Bank: <b>{escape(settings.bank_code)}</b>\n"
            f"💳 Account: <code>{escape(settings.bank_account)}</code>\n"
            f"👤 Account name: <b>{escape(settings.bank_account_name)}</b>\n"
            f"💰 Amount: <b>{format_vnd(amount)}</b>\n"
            f"🧾 Required content: <code>{deposit.code}</code>\n\n"
            "Keep the exact amount and content. Your balance updates automatically."
            "\n\n⏳ This QR is valid for 5 minutes. After that, the message is deleted and "
            "the payment request fails."
        )
        try:
            sent = await target.answer_photo(
                qr_url,
                caption=text,
                reply_markup=back_menu(user.language),
            )
        except TelegramBadRequest:
            sent = await target.answer(
                f'{text}\n\n<a href="{qr_url}">Mở mã QR / Open QR</a>',
                reply_markup=back_menu(user.language),
                disable_web_page_preview=True,
            )
        await register_deposit_message(
            session,
            deposit.id,
            sent.chat.id,
            sent.message_id,
        )

    @router.callback_query(F.data == "menu:orders")
    async def show_orders(callback: CallbackQuery, session: AsyncSession) -> None:
        await callback.answer()
        user = await get_or_create_user(callback, session)
        orders = await recent_orders(session, user.telegram_id, limit=40)
        if not orders:
            text = (
                "📦 Bạn chưa có đơn mua nào." if user.language == "vi" else "📦 You have no orders."
            )
            markup = back_menu(user.language)
        else:
            text = (
                "📦 <b>Đơn hàng đã mua</b>\n\nChọn một đơn để xem tài khoản, sao chép "
                "hoặc tải lại file TXT."
                if user.language == "vi"
                else "📦 <b>Purchased orders</b>\n\nChoose an order to view, copy, or "
                "download its TXT file."
            )
            markup = order_history_menu(orders, user.language)
        if callback.message:
            await callback.message.edit_text(text, reply_markup=markup)

    @router.callback_query(F.data == "menu:codes")
    async def show_codes(callback: CallbackQuery, session: AsyncSession) -> None:
        await callback.answer()
        user = await get_or_create_user(callback, session)
        orders = await recent_orders(session, user.telegram_id, limit=40)
        if not orders:
            text = "🔑 Bạn chưa có code nào." if user.language == "vi" else "🔑 You have no codes."
            markup = back_menu(user.language)
        else:
            text = (
                "🔑 <b>Tài khoản/code đã mua</b>\n\nChọn đơn để hiện thông tin và nút sao chép."
                if user.language == "vi"
                else "🔑 <b>Purchased accounts/codes</b>\n\nChoose an order to reveal and copy it."
            )
            markup = order_history_menu(orders, user.language)
        if callback.message:
            await callback.message.edit_text(text, reply_markup=markup)

    @router.callback_query(F.data.startswith("orderdetail:"))
    async def show_order_detail(callback: CallbackQuery, session: AsyncSession) -> None:
        user = await get_or_create_user(callback, session)
        order_id = int(callback.data.split(":", 1)[1])
        orders = await order_bundle(session, user.telegram_id, order_id)
        if not orders:
            await callback.answer("Không tìm thấy đơn hàng.", show_alert=True)
            return
        order_ids, shop_order_code, product_name, secrets, total_amount = bundle_values(
            orders, user
        )
        if callback.message:
            await callback.message.edit_text(
                delivery_text(
                    shop_order_code=shop_order_code,
                    product_name=product_name,
                    secrets=secrets,
                    total_amount=total_amount,
                    language=user.language,
                ),
                reply_markup=delivery_keyboard(
                    primary_order_id=min(order_ids),
                    secrets=secrets,
                    language=user.language,
                ),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("ordertxt:"))
    async def download_order_file(callback: CallbackQuery, session: AsyncSession) -> None:
        user = await get_or_create_user(callback, session)
        order_id = int(callback.data.split(":", 1)[1])
        orders = await order_bundle(session, user.telegram_id, order_id)
        if not orders:
            await callback.answer("Không tìm thấy đơn hàng.", show_alert=True)
            return
        order_ids, shop_order_code, product_name, secrets, total_amount = bundle_values(
            orders, user
        )
        if callback.message:
            await callback.message.answer_document(
                delivery_file(
                    shop_order_code=shop_order_code,
                    product_name=product_name,
                    secrets=secrets,
                    total_amount=total_amount,
                    language=user.language,
                ),
                caption=(
                    f"📄 File tài khoản của đơn <code>{escape(shop_order_code)}</code>"
                    if user.language == "vi"
                    else f"📄 Account file for order <code>{escape(shop_order_code)}</code>"
                ),
            )
        await callback.answer("Đã tạo file TXT" if user.language == "vi" else "TXT file ready")

    @router.callback_query(F.data == "menu:profile")
    async def show_profile(callback: CallbackQuery, session: AsyncSession) -> None:
        await callback.answer()
        user = await get_or_create_user(callback, session)
        if callback.message:
            await callback.message.edit_text(
                await profile_text(user, session),
                reply_markup=back_menu(user.language),
            )

    async def warehouse_api_text(user: User, client: ApiClient) -> str:
        status = (
            "Bị admin đình chỉ"
            if client.admin_blocked
            else "Đang bật" if client.active else "Người dùng tạm khóa"
        )
        if user.language == "en":
            status = (
                "Suspended by admin"
                if client.admin_blocked
                else "Enabled" if client.active else "Disabled by user"
            )
            return (
                "🔌 <b>Warehouse integration API</b>\n\n"
                f"• API ID: <code>{escape(client.api_id)}</code>\n"
                f"• Status: <b>{status}</b>\n"
                f"• Wallet balance: <b>{format_vnd(user.balance)}</b>\n"
                f"• Base URL: <code>{escape(settings.shop_api_base_url)}</code>\n"
                f"• Limit: <b>{client.rate_limit_per_minute} requests/minute</b>\n\n"
                "Use this API to synchronize products, prices and stock, then buy accounts "
                "automatically from another shop. The API uses this Telegram account's wallet."
            )
        return (
            "🔌 <b>API đấu kho hàng</b>\n\n"
            f"• API ID: <code>{escape(client.api_id)}</code>\n"
            f"• Trạng thái: <b>{status}</b>\n"
            f"• Số dư dùng mua hàng: <b>{format_vnd(user.balance)}</b>\n"
            f"• Base URL: <code>{escape(settings.shop_api_base_url)}</code>\n"
            f"• Giới hạn: <b>{client.rate_limit_per_minute} request/phút</b>\n\n"
            "API dùng để đồng bộ sản phẩm, giá, tồn kho và mua tài khoản tự động từ shop khác. "
            "Mọi đơn API trừ trực tiếp vào ví của nick Telegram này."
        )

    @router.callback_query(F.data == "menu:warehouse-api")
    async def show_warehouse_api(callback: CallbackQuery, session: AsyncSession) -> None:
        await callback.answer()
        user = await get_or_create_user(callback, session)
        client, new_secret = await ensure_api_client(
            session,
            user.telegram_id,
            cipher,
            settings.shop_api_rate_limit_per_minute,
        )
        await session.commit()
        if callback.message:
            await callback.message.edit_text(
                await warehouse_api_text(user, client),
                reply_markup=warehouse_api_menu(
                    user.language,
                    client.active,
                    warehouse_docs_url,
                    client.admin_blocked,
                ),
            )
            if new_secret:
                warning = (
                    "⚠️ <b>API Secret chỉ hiển thị lần này</b>\n"
                    f"<code>{escape(new_secret)}</code>\n\n"
                    "Hãy lưu lại ngay. Nếu mất, bạn cần bấm Đổi API Secret."
                    if user.language == "vi"
                    else "⚠️ <b>This API Secret is shown once</b>\n"
                    f"<code>{escape(new_secret)}</code>\n\n"
                    "Save it now. Rotate the secret if it is lost."
                )
                await callback.message.answer(warning)

    @router.callback_query(F.data == "warehouse-api:rotate")
    async def confirm_api_rotation(callback: CallbackQuery, session: AsyncSession) -> None:
        user = await get_or_create_user(callback, session)
        text = (
            "Đổi API Secret sẽ làm secret cũ mất hiệu lực ngay. Bạn chắc chắn muốn đổi?"
            if user.language == "vi"
            else "Rotating the API Secret immediately invalidates the old secret. Continue?"
        )
        if callback.message:
            await callback.message.edit_text(
                text,
                reply_markup=warehouse_api_rotate_confirmation(user.language),
            )
        await callback.answer()

    @router.callback_query(F.data == "warehouse-api:rotate-confirm")
    async def rotate_warehouse_api(callback: CallbackQuery, session: AsyncSession) -> None:
        user = await get_or_create_user(callback, session)
        client, secret = await rotate_api_secret(session, user.telegram_id, cipher)
        await session.commit()
        if callback.message:
            await callback.message.edit_text(
                await warehouse_api_text(user, client),
                reply_markup=warehouse_api_menu(
                    user.language,
                    client.active,
                    warehouse_docs_url,
                    client.admin_blocked,
                ),
            )
            await callback.message.answer(
                (
                    "✅ <b>API Secret mới</b>\n"
                    f"<code>{escape(secret)}</code>\n\nSecret cũ đã bị khóa. Hãy lưu secret mới ngay."
                    if user.language == "vi"
                    else "✅ <b>New API Secret</b>\n"
                    f"<code>{escape(secret)}</code>\n\nThe old secret is disabled. Save this one now."
                )
            )
        await callback.answer("Đã đổi API Secret" if user.language == "vi" else "Secret rotated")

    @router.callback_query(F.data == "warehouse-api:toggle")
    async def toggle_warehouse_api(callback: CallbackQuery, session: AsyncSession) -> None:
        user = await get_or_create_user(callback, session)
        client = await session.scalar(
            select(ApiClient)
            .where(ApiClient.owner_user_id == user.telegram_id)
            .with_for_update()
        )
        if client is None:
            client, _ = await ensure_api_client(
                session,
                user.telegram_id,
                cipher,
                settings.shop_api_rate_limit_per_minute,
            )
        if client.admin_blocked:
            await callback.answer(
                "API đang bị admin đình chỉ. Hãy liên hệ hỗ trợ.",
                show_alert=True,
            )
            return
        client.active = not client.active
        await session.commit()
        if callback.message:
            await callback.message.edit_text(
                await warehouse_api_text(user, client),
                reply_markup=warehouse_api_menu(
                    user.language,
                    client.active,
                    warehouse_docs_url,
                    client.admin_blocked,
                ),
            )
        await callback.answer()

    @router.callback_query(F.data == "warehouse-api:blocked")
    async def warehouse_api_blocked(callback: CallbackQuery) -> None:
        await callback.answer(
            "API đang bị admin đình chỉ. Hãy liên hệ hỗ trợ.",
            show_alert=True,
        )

    @router.callback_query(F.data == "menu:referral")
    async def show_referral(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
        await callback.answer()
        user = await get_or_create_user(callback, session)
        stats = await referral_stats(session, user.telegram_id)
        referral_url = (
            f"https://t.me/{await bot_username(bot)}?start=ref_{user.referral_code}"
        )
        commission_percent = settings.referral_commission_percent
        text = (
            f"🎁 <b>Giới thiệu bạn bè · Hoa hồng {commission_percent}%</b>\n\n"
            f"Link của bạn:\n<code>{escape(referral_url)}</code>\n\n"
            f"• Người đã mời: <b>{stats.invited_users}</b>\n"
            f"• Đơn đã nhận hoa hồng: <b>{stats.rewarded_orders}</b>\n"
            f"• Tổng hoa hồng: <b>{format_vnd(stats.total_commission)}</b>\n\n"
            f"Bạn nhận {commission_percent}% số tiền thực trả của mọi đơn thành công từ người được giới thiệu. "
            "Hoa hồng được cộng thẳng vào ví."
            if user.language == "vi"
            else f"🎁 <b>Refer friends · {commission_percent}% commission</b>\n\n"
            f"Your link:\n<code>{escape(referral_url)}</code>\n\n"
            f"• Invited users: <b>{stats.invited_users}</b>\n"
            f"• Rewarded orders: <b>{stats.rewarded_orders}</b>\n"
            f"• Total commission: <b>{format_vnd(stats.total_commission)}</b>"
        )
        if callback.message:
            await callback.message.edit_text(
                text,
                reply_markup=referral_menu(user.language, referral_url),
                disable_web_page_preview=True,
            )

    @router.callback_query(F.data == "menu:support")
    async def support(callback: CallbackQuery, session: AsyncSession) -> None:
        await callback.answer()
        user = await get_or_create_user(callback, session)
        text = (
            f"🆘 Cần hỗ trợ? Liên hệ @{escape(settings.support_username)} và gửi kèm mã đơn."
            if user.language == "vi"
            else f"🆘 Need help? Contact @{escape(settings.support_username)} with your order ID."
        )
        if callback.message:
            await callback.message.edit_text(text, reply_markup=back_menu(user.language))

    @router.callback_query(F.data == "menu:language")
    async def choose_language(callback: CallbackQuery, session: AsyncSession) -> None:
        await callback.answer()
        user = await get_or_create_user(callback, session)
        if callback.message:
            await callback.message.edit_text(
                "🌐 Chọn ngôn ngữ / Choose language", reply_markup=language_menu(user.language)
            )

    @router.callback_query(F.data.startswith("lang:"))
    async def set_language(callback: CallbackQuery, session: AsyncSession) -> None:
        user = await get_or_create_user(callback, session)
        language = callback.data.split(":", 1)[1]
        if language not in {"vi", "en"}:
            await callback.answer("Invalid language", show_alert=True)
            return
        user.language = language
        await session.commit()
        if callback.message:
            await callback.message.edit_text(
                home_text(user, settings),
                reply_markup=main_menu(
                    user.language,
                    sms_enabled=sms_enabled,
                ),
                disable_web_page_preview=True,
            )
        await callback.answer("Đã đổi ngôn ngữ" if language == "vi" else "Language changed")

    @router.callback_query(F.data == "menu:clear")
    async def clear_menu(
        callback: CallbackQuery,
        bot: Bot,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        await state.clear()
        user = await get_or_create_user(callback, session)
        if callback.message:
            await callback.answer("Đang dọn chat…" if user.language == "vi" else "Cleaning chat…")
            await delete_recent_messages(
                bot,
                chat_id=callback.message.chat.id,
                newest_message_id=callback.message.message_id,
            )
            await bot.send_message(
                callback.message.chat.id,
                home_text(user, settings),
                reply_markup=main_menu(
                    user.language,
                    sms_enabled=sms_enabled,
                ),
                disable_web_page_preview=True,
            )
            return
        await callback.answer()

    return router
