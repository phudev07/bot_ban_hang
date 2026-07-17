from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.filters.command import CommandObject
from aiogram.filters.exception import ExceptionMessageFilter, ExceptionTypeFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ErrorEvent, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat_cleanup import delete_recent_messages
from app.config import Settings
from app.delivery import (
    delivery_file,
    delivery_keyboard,
    delivery_text,
)
from app.keyboards import (
    back_menu,
    categories_menu,
    coupon_quantity_menu,
    deposit_amounts,
    language_menu,
    main_menu,
    order_history_menu,
    product_detail,
    products_menu,
    purchase_payment_options,
    quantity_menu,
    referral_menu,
    warehouse_api_menu,
    warehouse_api_rotate_confirmation,
)
from app.models import ApiClient, Product, User
from app.partner_services import ensure_api_client, referral_stats, rotate_api_secret
from app.payment_expiry import register_deposit_message
from app.services import (
    active_categories,
    active_products,
    available_stock,
    create_deposit,
    ensure_user,
    order_bundle,
    PendingDepositLimitReached,
    product_pricing,
    purchase_product,
    recent_orders,
    user_activity_stats,
)
from app.states import DepositStates, PurchaseStates
from app.suppliers import SumistoreClient
from app.utils import SecretCipher, build_sepay_qr_url, format_vnd, parse_vnd


def home_text(user: User, settings: Settings) -> str:
    username = f"@{escape(user.username)}" if user.username else "Chưa đặt"
    group_url = escape(settings.community_group_url, quote=True)
    group_line_en = (
        f'\n📢 Telegram group: <a href="{group_url}">Join group</a>' if group_url else ""
    )
    group_line_vi = f'\n📢 Nhóm Telegram: <a href="{group_url}">Vào nhóm</a>' if group_url else ""
    if user.language == "en":
        return (
            f"✨ Hello, <b>{escape(user.full_name)}</b>\n"
            f"• ID: <code>{user.telegram_id}</code>\n"
            f"• Username: {username}\n"
            f"👛 Available balance: <b>{format_vnd(user.balance)}</b>\n\n"
            "<b>Priority actions</b>\n"
            "• Quick buy, Deposit, My codes\n\n"
            f"💬 Support: @{escape(settings.support_username)}"
            f"{group_line_en}"
        )
    return (
        f"✨ Xin chào, <b>{escape(user.full_name)}</b>\n"
        f"• ID: <code>{user.telegram_id}</code>\n"
        f"• Username: {username}\n"
        f"👛 Số dư khả dụng: <b>{format_vnd(user.balance)}</b>\n\n"
        "<b>Ưu tiên trước</b>\n"
        "• Mua nhanh, Nạp tiền, Lấy code\n\n"
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


def create_router(
    settings: Settings,
    cipher: SecretCipher,
    supplier_client: SumistoreClient | None = None,
) -> Router:
    router = Router(name="customer")
    warehouse_docs_url = (
        f"{settings.shop_api_base_url.rstrip('/').removesuffix('/v1')}/docs"
    )

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
        product_name = (
            orders[0].product.name_en if user.language == "en" else orders[0].product.name_vi
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
        await message.answer(
            home_text(user, settings),
            reply_markup=main_menu(user.language),
            disable_web_page_preview=True,
        )

    @router.message(Command("muanhanh"))
    async def quick_buy_command(message: Message, session: AsyncSession) -> None:
        user = await get_or_create_user(message, session)
        products = await active_products(session)
        text = "⚡ <b>Mua nhanh</b>" if user.language == "vi" else "⚡ <b>Quick buy</b>"
        if not products:
            text = "Kho chưa có mặt hàng." if user.language == "vi" else "No products yet."
        await message.answer(
            text,
            reply_markup=products_menu(products, user.language, "back:menu"),
        )

    @router.message(Command("naptien"))
    async def deposit_command(message: Message, session: AsyncSession) -> None:
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
            reply_markup=main_menu(user.language),
            disable_web_page_preview=True,
        )

    @router.callback_query(F.data == "back:menu")
    async def back_to_menu(
        callback: CallbackQuery, session: AsyncSession, state: FSMContext
    ) -> None:
        await state.clear()
        user = await get_or_create_user(callback, session)
        if callback.message:
            await callback.message.edit_text(
                home_text(user, settings),
                reply_markup=main_menu(user.language),
                disable_web_page_preview=True,
            )
        await callback.answer()

    @router.callback_query(F.data == "menu:products")
    async def show_categories(callback: CallbackQuery, session: AsyncSession) -> None:
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
            await callback.message.edit_text(
                text, reply_markup=categories_menu(categories, user.language)
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("cat:"))
    async def show_products(callback: CallbackQuery, session: AsyncSession) -> None:
        user = await get_or_create_user(callback, session)
        category_id = int(callback.data.split(":", 1)[1])
        products = await active_products(session, category_id)
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
                reply_markup=products_menu(products, user.language, "menu:products"),
            )
        await callback.answer()

    @router.callback_query(F.data == "menu:quick")
    async def quick_buy(callback: CallbackQuery, session: AsyncSession) -> None:
        user = await get_or_create_user(callback, session)
        products = await active_products(session)
        text = "⚡ <b>Mua nhanh</b>" if user.language == "vi" else "⚡ <b>Quick buy</b>"
        if not products:
            text = "Kho chưa có mặt hàng." if user.language == "vi" else "No products yet."
        if callback.message:
            await callback.message.edit_text(
                text, reply_markup=products_menu(products, user.language, "back:menu")
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("prod:"))
    async def show_product_detail(callback: CallbackQuery, session: AsyncSession) -> None:
        user = await get_or_create_user(callback, session)
        product_id = int(callback.data.split(":", 1)[1])
        product = await session.get(Product, product_id)
        if product is None or not product.active:
            await callback.answer("Sản phẩm không tồn tại.", show_alert=True)
            return
        stock = await available_stock(
            session,
            product.id,
            supplier_client,
            refresh_external=True,
        )
        name = product.name_en if user.language == "en" else product.name_vi
        description = product.description_en if user.language == "en" else product.description_vi
        labels = (
            ("Price", "In stock", "Description")
            if user.language == "en"
            else ("Giá", "Còn hàng", "Thông tin")
        )
        text = (
            f"📦 <b>{escape(name)}</b>\n\n"
            f"📝 {labels[2]}: {escape(description or '—')}\n\n"
            f"💵 {labels[0]}: <b>{format_vnd(product.price)}</b>\n"
            f"📊 {labels[1]}: <b>{stock}</b>"
        )
        if callback.message:
            await callback.message.edit_text(
                text,
                reply_markup=product_detail(product, user.language, stock),
            )
        await callback.answer()

    async def complete_product_purchase(
        target: Message,
        user: User,
        product_id: int,
        quantity: int,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        coupon_id: int | None = None,
    ) -> str:
        result = await purchase_product(
            session_factory,
            user.telegram_id,
            product_id,
            cipher,
            quantity,
            supplier_client,
            coupon_id=coupon_id,
            referral_commission_percent=settings.referral_commission_percent,
        )
        messages_vi = {
            "out_of_stock": "Sản phẩm vừa hết hàng.",
            "blocked": "Tài khoản đang bị khóa. Liên hệ hỗ trợ.",
            "not_found": "Sản phẩm không tồn tại.",
            "invalid_quantity": "Số lượng không hợp lệ.",
            "supplier_unavailable": "Nguồn hàng đang tạm gián đoạn. Vui lòng thử lại sau.",
            "invalid_coupon": "Mã giảm giá không hợp lệ, đã hết hạn hoặc hết lượt sử dụng.",
        }
        messages_en = {
            "out_of_stock": "This product is out of stock.",
            "blocked": "Your account is blocked. Please contact support.",
            "not_found": "Product not found.",
            "invalid_quantity": "Invalid quantity.",
            "supplier_unavailable": "The supplier is temporarily unavailable. Please try again.",
            "invalid_coupon": "This discount code is invalid, expired, or fully used.",
        }
        if not result.ok:
            if result.message == "insufficient":
                product = await session.get(Product, product_id)
                if product is not None:
                    total_amount = result.total_amount or product.price * quantity
                    coupon_line_vi = (
                        f"Mã giảm giá: <b>{escape(result.coupon_code)}</b> "
                        f"(giảm {format_vnd(result.discount_amount)})\n"
                        if result.coupon_code
                        else ""
                    )
                    coupon_line_en = (
                        f"Discount code: <b>{escape(result.coupon_code)}</b> "
                        f"(-{format_vnd(result.discount_amount)})\n"
                        if result.coupon_code
                        else ""
                    )
                    text = (
                        "💳 <b>Số dư chưa đủ</b>\n\n"
                        f"Sản phẩm: <b>{escape(product.name_vi)}</b>\n"
                        f"Số lượng: <b>{quantity}</b>\n"
                        f"{coupon_line_vi}"
                        f"Tổng tiền: <b>{format_vnd(total_amount)}</b>\n"
                        f"Số dư hiện có: <b>{format_vnd(user.balance)}</b>\n\n"
                        "Bạn có thể thanh toán QR trực tiếp cho sản phẩm này. "
                        "Số dư hiện có vẫn được giữ nguyên."
                        if user.language == "vi"
                        else "💳 <b>Insufficient balance</b>\n\n"
                        f"Product: <b>{escape(product.name_en)}</b>\n"
                        f"Quantity: <b>{quantity}</b>\n"
                        f"{coupon_line_en}"
                        f"Total: <b>{format_vnd(total_amount)}</b>\n"
                        f"Current balance: <b>{format_vnd(user.balance)}</b>\n\n"
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
                        ),
                    )
                return result.message
            labels = messages_en if user.language == "en" else messages_vi
            await target.answer(labels.get(result.message, "Error"))
            return result.message
        if result.orders and result.secrets:
            product_name = (
                result.orders[0].product.name_en
                if user.language == "en"
                else result.orders[0].product.name_vi
            )
            order_ids = [order.id for order in result.orders]
            text = delivery_text(
                shop_order_code=result.orders[0].shop_order_code,
                product_name=product_name,
                secrets=result.secrets,
                total_amount=result.total_amount,
                language=user.language,
            )
            if result.coupon_code:
                coupon_note = (
                    f"\n\n🏷 Mã <b>{escape(result.coupon_code)}</b> đã giảm "
                    f"<b>{format_vnd(result.discount_amount)}</b>."
                    if user.language == "vi"
                    else f"\n\n🏷 Code <b>{escape(result.coupon_code)}</b> saved "
                    f"<b>{format_vnd(result.discount_amount)}</b>."
                )
                text += coupon_note
            await target.answer(
                text,
                reply_markup=delivery_keyboard(
                    primary_order_id=min(order_ids),
                    secrets=result.secrets,
                    language=user.language,
                ),
            )
        return result.message

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
        await state.set_state(PurchaseStates.waiting_for_coupon)
        await state.update_data(product_id=product.id)
        prompt = (
            f"🏷 <b>Nhập mã giảm giá</b>\n\nSản phẩm: <b>{escape(product.name_vi)}</b>\n"
            "Gửi mã giảm giá bạn muốn sử dụng."
            if user.language == "vi"
            else f"🏷 <b>Apply a discount code</b>\n\nProduct: "
            f"<b>{escape(product.name_en)}</b>\nSend the code you want to use."
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
        stock = await available_stock(
            session,
            product.id,
            supplier_client,
            refresh_external=True,
        )
        pricing = await product_pricing(
            session,
            product,
            coupon_code=message.text or "",
        )
        if pricing is None:
            await message.answer(
                "Mã không hợp lệ, đã hết hạn hoặc hết lượt. Hãy kiểm tra và nhập lại."
                if user.language == "vi"
                else "The code is invalid, expired, or fully used. Please try again."
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
            return
        text = (
            f"✅ <b>Đã áp dụng mã {escape(coupon.code)}</b>\n\n"
            f"• Giá gốc: <s>{format_vnd(pricing.original_unit_price)}</s>\n"
            f"• Giảm mỗi sản phẩm: <b>{format_vnd(pricing.discount_per_unit)}</b>\n"
            f"• Giá còn lại: <b>{format_vnd(pricing.final_unit_price)}</b>\n\n"
            "Chọn số lượng để mua với mức giá này."
            if user.language == "vi"
            else f"✅ <b>Code {escape(coupon.code)} applied</b>\n\n"
            f"• Original: <s>{format_vnd(pricing.original_unit_price)}</s>\n"
            f"• Discount per item: <b>{format_vnd(pricing.discount_per_unit)}</b>\n"
            f"• Final price: <b>{format_vnd(pricing.final_unit_price)}</b>\n\n"
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
        product_id = int(callback.data.split(":", 1)[1])
        product = await session.get(Product, product_id)
        if product is None or not product.active:
            await callback.answer("Sản phẩm không tồn tại.", show_alert=True)
            return
        stock = await available_stock(
            session,
            product.id,
            supplier_client,
            refresh_external=True,
        )
        if stock <= 0:
            if callback.message:
                await callback.message.edit_reply_markup(
                    reply_markup=product_detail(product, user.language, 0)
                )
            await callback.answer("Sản phẩm đã hết hàng.", show_alert=True)
            return
        text = (
            f"🧮 <b>Chọn số lượng</b>\n\n"
            f"• Sản phẩm: <b>{escape(product.name_vi)}</b>\n"
            f"• Đơn giá: <b>{format_vnd(product.price)}</b>\n"
            f"• Còn hàng: <b>{stock}</b>\n"
            f"• Tối đa mỗi lần: <b>{product.max_quantity}</b>"
            if user.language == "vi"
            else f"🧮 <b>Choose quantity</b>\n\n"
            f"• Product: <b>{escape(product.name_en)}</b>\n"
            f"• Unit price: <b>{format_vnd(product.price)}</b>\n"
            f"• In stock: <b>{stock}</b>\n"
            f"• Maximum per order: <b>{product.max_quantity}</b>"
        )
        if callback.message:
            await callback.message.edit_text(
                text,
                reply_markup=quantity_menu(product, user.language, stock),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("customqty:"))
    async def custom_purchase_quantity(
        callback: CallbackQuery,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        user = await get_or_create_user(callback, session)
        product_id = int(callback.data.split(":", 1)[1])
        product = await session.get(Product, product_id)
        if product is None or not product.active or not product.allow_quantity:
            await callback.answer("Sản phẩm không hợp lệ.", show_alert=True)
            return
        stock = await available_stock(
            session,
            product.id,
            supplier_client,
            refresh_external=True,
        )
        if stock <= 0:
            await callback.answer("Sản phẩm đã hết hàng.", show_alert=True)
            return
        maximum = min(product.max_quantity, stock)
        await state.set_state(PurchaseStates.waiting_for_quantity)
        await state.update_data(product_id=product.id, maximum_quantity=maximum)
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
        await complete_product_purchase(
            message,
            user,
            product.id,
            quantity,
            session,
            session_factory,
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
        pricing = await product_pricing(session, product, coupon_id=int(coupon_id_text))
        if pricing is None:
            await callback.answer("Mã giảm giá không còn hiệu lực.", show_alert=True)
            return
        stock = await available_stock(
            session,
            product.id,
            supplier_client,
            refresh_external=True,
        )
        maximum = min(product.max_quantity, stock)
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
        if callback.message:
            await complete_product_purchase(
                callback.message,
                user,
                product_id,
                quantity,
                session,
                session_factory,
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("buycoupon:"))
    async def buy_product_with_coupon(
        callback: CallbackQuery,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await get_or_create_user(callback, session)
        _, product_id_text, quantity_text, coupon_id_text = callback.data.split(":")
        if callback.message:
            await complete_product_purchase(
                callback.message,
                user,
                int(product_id_text),
                int(quantity_text),
                session,
                session_factory,
                int(coupon_id_text),
            )
        await callback.answer()

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
        coupon_id = int(parts[3]) if len(parts) > 3 else None
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
        if (
            await available_stock(
                session,
                product.id,
                supplier_client,
                refresh_external=True,
            )
            < quantity
        ):
            await callback.answer("Sản phẩm vừa hết hàng.", show_alert=True)
            return

        pricing = await product_pricing(session, product, coupon_id=coupon_id)
        if pricing is None:
            await callback.answer("Mã giảm giá không còn hiệu lực.", show_alert=True)
            return
        total_amount = pricing.final_unit_price * quantity
        total_discount = pricing.discount_per_unit * quantity
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
                expiry_seconds=settings.payment_expiry_seconds,
                max_pending_deposits=settings.max_pending_deposits_per_user,
            )
        except PendingDepositLimitReached:
            await callback.answer(
                "Bạn đang có quá nhiều QR chờ thanh toán. Hãy dùng QR cũ hoặc chờ hết hạn.",
                show_alert=True,
            )
            return
        qr_url = build_sepay_qr_url(
            settings.bank_code,
            settings.bank_account,
            total_amount,
            deposit.code,
        )
        product_name = product.name_en if user.language == "en" else product.name_vi
        coupon_line_vi = (
            f"• Mã giảm giá: <b>{escape(pricing.coupon.code)}</b> "
            f"(-{format_vnd(total_discount)})\n"
            if pricing.coupon
            else ""
        )
        coupon_line_en = (
            f"• Discount code: <b>{escape(pricing.coupon.code)}</b> "
            f"(-{format_vnd(total_discount)})\n"
            if pricing.coupon
            else ""
        )
        text = (
            "🧾 <b>Thanh toán sản phẩm</b>\n\n"
            f"• Sản phẩm: <b>{escape(product_name)}</b>\n"
            f"• Số lượng: <b>{quantity}</b>\n"
            f"{coupon_line_vi}"
            f"• Ngân hàng: <b>{escape(settings.bank_code)}</b>\n"
            f"• Số tài khoản: <code>{escape(settings.bank_account)}</code>\n"
            f"• Chủ tài khoản: <b>{escape(settings.bank_account_name)}</b>\n"
            f"• Số tiền: <b>{format_vnd(total_amount)}</b>\n"
            f"• Nội dung bắt buộc: <code>{deposit.code}</code>\n\n"
            "Giữ nguyên số tiền và nội dung. Sản phẩm sẽ được giao tự động sau khi "
            "giao dịch được ghi nhận."
            "\n\n⏳ QR chỉ có hiệu lực 5 phút. Quá hạn bot sẽ xóa tin nhắn và giao dịch thất bại."
            if user.language == "vi"
            else "🧾 <b>Product payment</b>\n\n"
            f"• Product: <b>{escape(product_name)}</b>\n"
            f"• Quantity: <b>{quantity}</b>\n"
            f"{coupon_line_en}"
            f"• Bank: <b>{escape(settings.bank_code)}</b>\n"
            f"• Account: <code>{escape(settings.bank_account)}</code>\n"
            f"• Account name: <b>{escape(settings.bank_account_name)}</b>\n"
            f"• Amount: <b>{format_vnd(total_amount)}</b>\n"
            f"• Required content: <code>{deposit.code}</code>\n\n"
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
            f"• Ngân hàng: <b>{escape(settings.bank_code)}</b>\n"
            f"• Số tài khoản: <code>{escape(settings.bank_account)}</code>\n"
            f"• Chủ tài khoản: <b>{escape(settings.bank_account_name)}</b>\n"
            f"• Số tiền: <b>{format_vnd(amount)}</b>\n"
            f"• Nội dung bắt buộc: <code>{deposit.code}</code>\n\n"
            "Giữ nguyên số tiền và nội dung. Số dư sẽ được cập nhật tự động."
            "\n\n⏳ QR chỉ có hiệu lực 5 phút. Quá hạn bot sẽ xóa tin nhắn và giao dịch thất bại."
            if user.language == "vi"
            else "💳 <b>Bank transfer details</b>\n\n"
            f"• Bank: <b>{escape(settings.bank_code)}</b>\n"
            f"• Account: <code>{escape(settings.bank_account)}</code>\n"
            f"• Account name: <b>{escape(settings.bank_account_name)}</b>\n"
            f"• Amount: <b>{format_vnd(amount)}</b>\n"
            f"• Required content: <code>{deposit.code}</code>\n\n"
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
        await callback.answer()

    @router.callback_query(F.data == "menu:codes")
    async def show_codes(callback: CallbackQuery, session: AsyncSession) -> None:
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
        await callback.answer()

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
        user = await get_or_create_user(callback, session)
        if callback.message:
            await callback.message.edit_text(
                await profile_text(user, session),
                reply_markup=back_menu(user.language),
            )
        await callback.answer()

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
        await callback.answer()

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
        user = await get_or_create_user(callback, session)
        stats = await referral_stats(session, user.telegram_id)
        bot_user = await bot.get_me()
        referral_url = f"https://t.me/{bot_user.username}?start=ref_{user.referral_code}"
        text = (
            "🎁 <b>Giới thiệu bạn bè · Hoa hồng 5%</b>\n\n"
            f"Link của bạn:\n<code>{escape(referral_url)}</code>\n\n"
            f"• Người đã mời: <b>{stats.invited_users}</b>\n"
            f"• Đơn đã nhận hoa hồng: <b>{stats.rewarded_orders}</b>\n"
            f"• Tổng hoa hồng: <b>{format_vnd(stats.total_commission)}</b>\n\n"
            "Bạn nhận 5% số tiền thực trả của mọi đơn thành công từ người được giới thiệu. "
            "Hoa hồng được cộng thẳng vào ví."
            if user.language == "vi"
            else "🎁 <b>Refer friends · 5% commission</b>\n\n"
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
        await callback.answer()

    @router.callback_query(F.data == "menu:support")
    async def support(callback: CallbackQuery, session: AsyncSession) -> None:
        user = await get_or_create_user(callback, session)
        text = (
            f"🆘 Cần hỗ trợ? Liên hệ @{escape(settings.support_username)} và gửi kèm mã đơn."
            if user.language == "vi"
            else f"🆘 Need help? Contact @{escape(settings.support_username)} with your order ID."
        )
        if callback.message:
            await callback.message.edit_text(text, reply_markup=back_menu(user.language))
        await callback.answer()

    @router.callback_query(F.data == "menu:language")
    async def choose_language(callback: CallbackQuery, session: AsyncSession) -> None:
        user = await get_or_create_user(callback, session)
        if callback.message:
            await callback.message.edit_text(
                "🌐 Chọn ngôn ngữ / Choose language", reply_markup=language_menu(user.language)
            )
        await callback.answer()

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
                reply_markup=main_menu(user.language),
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
                reply_markup=main_menu(user.language),
                disable_web_page_preview=True,
            )
            return
        await callback.answer()

    return router
