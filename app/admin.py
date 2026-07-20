from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.broadcasts import queue_broadcast
from app.config import Settings
from app.models import Category, InventoryItem, Order, Product, User
from app.states import BroadcastStates
from app.suppliers import EXTERNAL_FULFILLMENT_SOURCES
from app.utils import SecretCipher, format_vnd


def create_admin_router(settings: Settings, cipher: SecretCipher) -> Router:
    router = Router(name="admin")

    def is_admin_id(user_id: int | None) -> bool:
        return bool(user_id is not None and user_id in settings.admin_ids)

    def is_admin(message: Message) -> bool:
        return bool(message.from_user and is_admin_id(message.from_user.id))

    def broadcast_confirmation_keyboard(recipient_count: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"📤 Gửi tới {recipient_count} người",
                        callback_data="broadcast:confirm",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Hủy",
                        callback_data="broadcast:cancel",
                    )
                ],
            ]
        )

    async def reject_if_not_admin(message: Message) -> bool:
        if is_admin(message):
            return False
        await message.answer("Bạn không có quyền dùng lệnh này.")
        return True

    @router.message(Command("admin"))
    async def admin_panel(message: Message, session: AsyncSession) -> None:
        if await reject_if_not_admin(message):
            return
        users = int(await session.scalar(select(func.count(User.telegram_id))) or 0)
        batch_orders = int(
            await session.scalar(
                select(func.count(func.distinct(Order.batch_code))).where(
                    Order.batch_code.is_not(None)
                )
            )
            or 0
        )
        single_orders = int(
            await session.scalar(
                select(func.count(Order.id)).where(Order.batch_code.is_(None))
            )
            or 0
        )
        orders = batch_orders + single_orders
        revenue = int(await session.scalar(select(func.coalesce(func.sum(Order.amount), 0))) or 0)
        stock = int(
            await session.scalar(
                select(func.count(InventoryItem.id)).where(InventoryItem.status == "available")
            )
            or 0
        )
        await message.answer(
            "🛠 <b>Quản trị</b>\n\n"
            f"• Người dùng: {users}\n"
            f"• Đơn thành công: {orders}\n"
            f"• Doanh thu: {format_vnd(revenue)}\n"
            f"• Hàng còn: {stock}\n\n"
            "<b>Lệnh</b>\n"
            "/products\n"
            "/addcategory Tên danh mục\n"
            "/addproduct category_id | tên | giá | mô tả\n"
            "/addstock product_id rồi xuống dòng nhập hàng; ngăn các món bằng dòng ---\n"
            "/thongbao - gửi thông báo tới tất cả người đã /start"
        )

    async def stage_broadcast(
        source: Message,
        admin_message: Message,
        bot: Bot,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        recipient_count = int(
            await session.scalar(
                select(func.count(User.telegram_id)).where(User.has_started.is_(True))
            )
            or 0
        )
        if recipient_count == 0:
            await state.clear()
            await admin_message.answer("Chưa có người dùng nào đã /start để nhận thông báo.")
            return
        await state.set_state(BroadcastStates.waiting_for_confirmation)
        await state.update_data(
            source_chat_id=source.chat.id,
            source_message_id=source.message_id,
            recipient_count=recipient_count,
        )
        keyboard = broadcast_confirmation_keyboard(recipient_count)
        try:
            await bot.copy_message(
                chat_id=admin_message.chat.id,
                from_chat_id=source.chat.id,
                message_id=source.message_id,
                reply_markup=keyboard,
            )
        except TelegramBadRequest:
            await admin_message.answer(
                "📣 <b>Xác nhận gửi thông báo</b>\n\n"
                f"• Người nhận dự kiến: <b>{recipient_count}</b>\n"
                "• Nội dung sẽ được copy nguyên định dạng/media.\n"
                "• Mỗi tin có thêm nút 🛒 Mua ngay.\n\n"
                "Bấm Gửi để bắt đầu hoặc Hủy để bỏ.",
                reply_markup=keyboard,
            )

    @router.message(Command("thongbao"))
    async def begin_broadcast(
        message: Message,
        bot: Bot,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        if await reject_if_not_admin(message):
            return
        await state.clear()
        if message.reply_to_message is not None:
            await stage_broadcast(message.reply_to_message, message, bot, session, state)
            return
        await state.set_state(BroadcastStates.waiting_for_content)
        await message.answer(
            "📣 Gửi tin nhắn, ảnh, video hoặc file bạn muốn phát thông báo.\n\n"
            "Bot sẽ tạo bản xem trước và đặt nút Gửi ngay bên dưới nội dung."
        )

    @router.message(BroadcastStates.waiting_for_content)
    async def receive_broadcast_content(
        message: Message,
        bot: Bot,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        if await reject_if_not_admin(message):
            await state.clear()
            return
        await stage_broadcast(message, message, bot, session, state)

    @router.callback_query(
        BroadcastStates.waiting_for_confirmation,
        F.data == "broadcast:cancel",
    )
    async def cancel_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
        if not is_admin_id(callback.from_user.id if callback.from_user else None):
            await callback.answer("Bạn không có quyền.", show_alert=True)
            return
        await state.clear()
        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
            await callback.message.answer("Đã hủy thông báo.")
        await callback.answer()

    @router.callback_query(
        BroadcastStates.waiting_for_confirmation,
        F.data == "broadcast:confirm",
    )
    async def confirm_broadcast(
        callback: CallbackQuery,
        session_factory: async_sessionmaker[AsyncSession],
        state: FSMContext,
    ) -> None:
        admin_id = callback.from_user.id if callback.from_user else None
        if not is_admin_id(admin_id):
            await callback.answer("Bạn không có quyền.", show_alert=True)
            return
        data = await state.get_data()
        source_chat_id = int(data.get("source_chat_id", 0))
        source_message_id = int(data.get("source_message_id", 0))
        if not source_chat_id or not source_message_id:
            await state.clear()
            await callback.answer("Nội dung thông báo đã hết hạn.", show_alert=True)
            return

        await state.clear()
        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
        queued = await queue_broadcast(
            session_factory,
            admin_id=int(admin_id),
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
        )
        await callback.answer("Đã đưa vào hàng chờ.")
        if callback.message:
            await callback.message.answer(
                "✅ <b>Đã đưa thông báo vào hàng chờ</b>\n\n"
                f"• Mã lần gửi: <code>#{queued.broadcast_id}</code>\n"
                f"• Người nhận: <b>{queued.total}</b>\n\n"
                "Xem tốc độ và kết quả trong trang Admin → Thông báo."
            )

    @router.message(Command("products"))
    async def list_products(message: Message, session: AsyncSession) -> None:
        if await reject_if_not_admin(message):
            return
        products = list(await session.scalars(select(Product).order_by(Product.id)))
        if not products:
            await message.answer("Chưa có sản phẩm.")
            return
        lines = ["📦 <b>Danh sách sản phẩm</b>"]
        for product in products:
            if product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES:
                stock = max(0, product.external_stock)
            else:
                stock = int(
                    await session.scalar(
                        select(func.count(InventoryItem.id)).where(
                            InventoryItem.product_id == product.id,
                            InventoryItem.status == "available",
                        )
                    )
                    or 0
                )
            lines.append(
                f"\n<code>#{product.id}</code> {escape(product.name_vi)} · "
                f"{format_vnd(product.price)} · kho {stock}"
            )
        await message.answer("\n".join(lines))

    @router.message(Command("addcategory"))
    async def add_category(message: Message, session: AsyncSession) -> None:
        if await reject_if_not_admin(message):
            return
        name = (message.text or "").partition(" ")[2].strip()
        if not name:
            await message.answer("Cú pháp: /addcategory Tên danh mục")
            return
        category = Category(name_vi=name, name_en=name)
        session.add(category)
        await session.commit()
        await message.answer(f"Đã tạo danh mục <code>#{category.id}</code> {escape(name)}.")

    @router.message(Command("addproduct"))
    async def add_product(message: Message, session: AsyncSession) -> None:
        if await reject_if_not_admin(message):
            return
        raw = (message.text or "").partition(" ")[2]
        parts = [part.strip() for part in raw.split("|", 3)]
        if len(parts) != 4 or not parts[0].isdigit() or not parts[2].replace(".", "").isdigit():
            await message.answer("Cú pháp: /addproduct category_id | tên | giá | mô tả")
            return
        category = await session.get(Category, int(parts[0]))
        if category is None:
            await message.answer("Không tìm thấy danh mục.")
            return
        price = int(parts[2].replace(".", ""))
        product = Product(
            category_id=category.id,
            name_vi=parts[1],
            name_en=parts[1],
            price=price,
            description_vi=parts[3],
            description_en=parts[3],
        )
        session.add(product)
        await session.commit()
        await message.answer(
            f"Đã tạo sản phẩm <code>#{product.id}</code> {escape(product.name_vi)}."
        )

    @router.message(Command("addstock"))
    async def add_stock(message: Message, session: AsyncSession) -> None:
        if await reject_if_not_admin(message):
            return
        raw = (message.text or "").partition(" ")[2].strip()
        first_line, separator, stock_text = raw.partition("\n")
        if not separator or not first_line.isdigit() or not stock_text.strip():
            await message.answer(
                "Cú pháp:\n<code>/addstock 1\naccount:password\n---\nkey-thu-hai</code>"
            )
            return
        product = await session.get(Product, int(first_line))
        if product is None:
            await message.answer("Không tìm thấy sản phẩm.")
            return
        items = [item.strip() for item in stock_text.split("\n---\n") if item.strip()]
        session.add_all(
            [
                InventoryItem(product_id=product.id, encrypted_secret=cipher.encrypt(item))
                for item in items
            ]
        )
        await session.commit()
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(f"Đã thêm {len(items)} món vào kho của {escape(product.name_vi)}.")

    return router
