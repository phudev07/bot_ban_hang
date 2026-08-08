from html import escape
import logging

from aiogram import Bot, F, Router
from aiogram.enums import MessageEntityType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.broadcasts import queue_broadcast
from app.config import Settings
from app.inventory_dedup import filter_duplicate_inventory
from app.models import Category, InventoryItem, Order, Product, User
from app.states import BroadcastStates, ProductDescriptionStates
from app.suppliers import EXTERNAL_FULFILLMENT_SOURCES
from app.utils import (
    SecretCipher,
    contains_supplier_identity,
    format_vnd,
    parse_vnd,
    safe_customer_telegram_html,
)


logger = logging.getLogger(__name__)


def custom_emoji_ids(message: Message) -> list[str]:
    entities = [*(message.entities or []), *(message.caption_entities or [])]
    return list(
        dict.fromkeys(
            entity.custom_emoji_id
            for entity in entities
            if entity.type == MessageEntityType.CUSTOM_EMOJI
            and entity.custom_emoji_id
        )
    )


class AdminCustomEmojiFilter(BaseFilter):
    def __init__(self, admin_ids: set[int]) -> None:
        self.admin_ids = admin_ids

    async def __call__(self, message: Message) -> bool:
        return bool(
            message.from_user
            and message.from_user.id in self.admin_ids
            and custom_emoji_ids(message)
        )


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
                        text=f"🛒 Gửi có Mua ngay · {recipient_count} người",
                        callback_data="broadcast:confirm:buy",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=f"📣 Chỉ gửi thông tin · {recipient_count} người",
                        callback_data="broadcast:confirm:info",
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

    def admin_panel_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✍️ Sửa mô tả sản phẩm trong bot",
                        callback_data="admin:description:list",
                    )
                ]
            ]
        )

    def description_product_keyboard(products: list[Product]) -> InlineKeyboardMarkup:
        rows = [
            [
                InlineKeyboardButton(
                    text=f"✍️ #{product.id} · {product.name_vi[:42]}",
                    callback_data=f"admin:description:select:{product.id}",
                )
            ]
            for product in products
        ]
        rows.append(
            [
                InlineKeyboardButton(
                    text="❌ Hủy",
                    callback_data="admin:description:cancel",
                )
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def description_language_keyboard(product_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🇻🇳 Mô tả tiếng Việt",
                        callback_data=f"admin:description:lang:{product_id}:vi",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🇺🇸 Mô tả tiếng Anh",
                        callback_data=f"admin:description:lang:{product_id}:en",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="← Chọn sản phẩm khác",
                        callback_data="admin:description:list",
                    ),
                    InlineKeyboardButton(
                        text="❌ Hủy",
                        callback_data="admin:description:cancel",
                    ),
                ],
            ]
        )

    async def active_description_products(session: AsyncSession) -> list[Product]:
        return list(
            await session.scalars(
                select(Product)
                .where(
                    Product.archived_at.is_(None),
                    Product.active.is_(True),
                    Product.product_type == "account",
                )
                .order_by(Product.id)
                .limit(90)
            )
        )

    async def send_description_product_list(
        message: Message,
        session: AsyncSession,
    ) -> None:
        products = await active_description_products(session)
        if not products:
            await message.answer(
                "Không có sản phẩm đang hiển thị để sửa mô tả. "
                "Hãy bật sản phẩm trong trang Admin trước."
            )
            return
        await message.answer(
            "✍️ <b>Sửa mô tả hiển thị trong bot</b>\n\n"
            "Chọn sản phẩm. Sau đó bạn chỉ cần gửi một tin nhắn đã định dạng "
            "và gắn emoji Premium; bot sẽ lưu nguyên format của Telegram.",
            reply_markup=description_product_keyboard(products),
        )

    async def reject_if_not_admin(message: Message) -> bool:
        if is_admin(message):
            return False
        await message.answer("Bạn không có quyền dùng lệnh này.")
        return True

    @router.message(Command("admin"))
    async def admin_panel(
        message: Message,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        if await reject_if_not_admin(message):
            return
        await state.clear()
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
            "/addstock product_id | giá vốn rồi xuống dòng nhập hàng; ngăn bằng ---\n"
            "/thongbao - gửi thông báo tới tất cả người đã /start\n"
            "/mota - sửa mô tả và emoji Telegram của sản phẩm",
            reply_markup=admin_panel_keyboard(),
        )

    @router.message(Command("mota"))
    async def begin_product_description(
        message: Message,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        if await reject_if_not_admin(message):
            return
        await state.clear()
        await send_description_product_list(message, session)

    @router.callback_query(F.data == "admin:description:list")
    async def open_product_description_list(
        callback: CallbackQuery,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        if not is_admin_id(callback.from_user.id if callback.from_user else None):
            await callback.answer("Bạn không có quyền.", show_alert=True)
            return
        await state.clear()
        if callback.message:
            await send_description_product_list(callback.message, session)
        await callback.answer()

    @router.callback_query(F.data.startswith("admin:description:select:"))
    async def select_description_product(
        callback: CallbackQuery,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        if not is_admin_id(callback.from_user.id if callback.from_user else None):
            await callback.answer("Bạn không có quyền.", show_alert=True)
            return
        product_id_text = (callback.data or "").rsplit(":", 1)[-1]
        product = (
            await session.get(Product, int(product_id_text))
            if product_id_text.isdigit()
            else None
        )
        if (
            product is None
            or product.archived_at is not None
            or not product.active
            or product.product_type != "account"
        ):
            await callback.answer("Sản phẩm không còn hiển thị.", show_alert=True)
            return
        await state.clear()
        if callback.message:
            await callback.message.answer(
                f"📦 <b>{escape(product.name_vi)}</b>\n\n"
                "Bạn muốn thay mô tả ngôn ngữ nào?",
                reply_markup=description_language_keyboard(product.id),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("admin:description:lang:"))
    async def select_description_language(
        callback: CallbackQuery,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        if not is_admin_id(callback.from_user.id if callback.from_user else None):
            await callback.answer("Bạn không có quyền.", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        product_id_text = parts[3] if len(parts) == 5 else ""
        language = parts[4] if len(parts) == 5 else ""
        product = (
            await session.get(Product, int(product_id_text))
            if product_id_text.isdigit()
            else None
        )
        if (
            product is None
            or product.archived_at is not None
            or not product.active
            or product.product_type != "account"
            or language not in {"vi", "en"}
        ):
            await callback.answer("Lựa chọn đã hết hạn.", show_alert=True)
            return
        await state.set_state(ProductDescriptionStates.waiting_for_content)
        await state.update_data(product_id=product.id, language=language)
        if callback.message:
            await callback.message.answer(
                "✍️ <b>Gửi mô tả mới ngay trong tin nhắn tiếp theo</b>\n\n"
                f"• Sản phẩm: <b>{escape(product.name_vi)}</b>\n"
                f"• Ngôn ngữ: <b>{'Tiếng Việt' if language == 'vi' else 'Tiếng Anh'}</b>\n\n"
                "Hãy soạn tin bằng công cụ định dạng của Telegram và chọn emoji Premium "
                "như khi nhắn tin bình thường. Bot sẽ giữ nguyên chữ đậm, nghiêng, "
                "gạch chân, link và custom emoji.\n\n"
                "Chỉ gửi phần mô tả, không cần gửi tên hay giá sản phẩm.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="❌ Hủy sửa mô tả",
                                callback_data="admin:description:cancel",
                            )
                        ]
                    ]
                ),
            )
        await callback.answer("Hãy gửi mô tả mới.")

    @router.message(ProductDescriptionStates.waiting_for_content)
    async def receive_product_description(
        message: Message,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        if await reject_if_not_admin(message):
            await state.clear()
            return
        plain_text = message.text or message.caption or ""
        if not plain_text.strip():
            await message.answer(
                "Mô tả phải là tin nhắn chữ hoặc caption. Hãy gửi lại, hoặc bấm Hủy."
            )
            return
        if plain_text.lstrip().startswith("/"):
            await message.answer(
                "Bạn đang ở bước gửi mô tả. Hãy gửi nội dung mô tả hoặc bấm "
                "Hủy sửa mô tả trước khi dùng lệnh khác."
            )
            return
        if len(plain_text) > 3000:
            await message.answer(
                "Mô tả dài quá 3.000 ký tự. Hãy rút gọn để bot còn chỗ hiển thị "
                "tên, giá, tồn kho và ưu đãi."
            )
            return
        source_html = message.html_text
        if contains_supplier_identity(source_html):
            await message.answer(
                "Mô tả có tên, URL hoặc mã kỹ thuật của nguồn hàng. "
                "Hãy bỏ thông tin nguồn rồi gửi lại."
            )
            return
        description_html = safe_customer_telegram_html(source_html).strip()
        if not description_html:
            await message.answer("Mô tả không có nội dung hợp lệ. Hãy gửi lại.")
            return
        state_data = await state.get_data()
        product_id = int(state_data.get("product_id", 0))
        language = str(state_data.get("language", ""))
        product = await session.scalar(
            select(Product).where(Product.id == product_id).with_for_update()
        )
        if (
            product is None
            or product.archived_at is not None
            or not product.active
            or product.product_type != "account"
            or language not in {"vi", "en"}
        ):
            await state.clear()
            await message.answer("Sản phẩm không còn hiển thị. Thao tác đã được hủy.")
            return
        if language == "vi":
            product.description_vi = description_html
        else:
            product.description_en = description_html
        await session.commit()
        await state.clear()
        await message.answer(
            "✅ <b>Đã cập nhật mô tả trong bot</b>\n\n"
            f"• Sản phẩm: <b>{escape(product.name_vi)}</b>\n"
            f"• Ngôn ngữ: <b>{'Tiếng Việt' if language == 'vi' else 'Tiếng Anh'}</b>\n\n"
            "📋 <b>Nội dung vừa lưu:</b>\n"
            f"{description_html}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✍️ Sửa tiếp sản phẩm này",
                            callback_data=f"admin:description:select:{product.id}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="📦 Chọn sản phẩm khác",
                            callback_data="admin:description:list",
                        )
                    ],
                ]
            ),
        )

    @router.callback_query(F.data == "admin:description:cancel")
    async def cancel_product_description(
        callback: CallbackQuery,
        state: FSMContext,
    ) -> None:
        if not is_admin_id(callback.from_user.id if callback.from_user else None):
            await callback.answer("Bạn không có quyền.", show_alert=True)
            return
        await state.clear()
        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
            await callback.message.answer("Đã hủy sửa mô tả sản phẩm.")
        await callback.answer()

    async def stage_broadcast(
        source: Message,
        admin_message: Message,
        bot: Bot,
        session: AsyncSession,
        state: FSMContext,
    ) -> None:
        source_parts = [
            getattr(source, "text", None),
            getattr(source, "caption", None),
        ]
        for media_field in ("document", "audio", "video", "animation"):
            media = getattr(source, media_field, None)
            source_parts.append(getattr(media, "file_name", None))
        source_text = "\n".join(part for part in source_parts if part)
        if contains_supplier_identity(source_text):
            await state.clear()
            await admin_message.answer(
                "Không thể gửi thông báo có tên nguồn hàng, URL nguồn hoặc mã lỗi kỹ thuật. "
                "Hãy đổi nội dung sang cách gọi chung của shop."
            )
            return
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
                "• Chọn gửi kèm nút 🛒 Mua ngay hoặc chỉ gửi nội dung.\n\n"
                "Chọn cách gửi bên dưới hoặc Hủy để bỏ.",
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
        F.data.in_({"broadcast:confirm:buy", "broadcast:confirm:info"}),
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
        include_purchase_button = callback.data == "broadcast:confirm:buy"

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
            include_purchase_button=include_purchase_button,
        )
        await callback.answer("Đã đưa vào hàng chờ.")
        if callback.message:
            await callback.message.answer(
                "✅ <b>Đã đưa thông báo vào hàng chờ</b>\n\n"
                f"• Mã lần gửi: <code>#{queued.broadcast_id}</code>\n"
                f"• Người nhận: <b>{queued.total}</b>\n\n"
                f"• Kiểu gửi: <b>{'Có nút Mua ngay' if include_purchase_button else 'Chỉ thông tin'}</b>\n\n"
                "Xem tốc độ và kết quả trong trang Admin → Thông báo."
            )

    @router.message(Command("products"))
    async def list_products(message: Message, session: AsyncSession) -> None:
        if await reject_if_not_admin(message):
            return
        products = list(
            await session.scalars(
                select(Product)
                .where(Product.archived_at.is_(None))
                .order_by(Product.id)
            )
        )
        if not products:
            await message.answer("Chưa có sản phẩm.")
            return
        lines = ["📦 <b>Danh sách sản phẩm</b>"]
        for product in products:
            if product.force_out_of_stock:
                stock = 0
            elif product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES:
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
        header = [part.strip() for part in first_line.split("|", 1)]
        cost_amount = parse_vnd(header[1]) if len(header) == 2 else None
        if (
            not separator
            or len(header) != 2
            or not header[0].isdigit()
            or cost_amount is None
            or not stock_text.strip()
        ):
            await message.answer(
                "Cú pháp:\n<code>/addstock 4 | 35.000\n"
                "account:password\n---\nkey-thu-hai</code>"
            )
            return
        product = await session.scalar(
            select(Product).where(Product.id == int(header[0])).with_for_update()
        )
        if product is None:
            await message.answer("Không tìm thấy sản phẩm.")
            return
        items = [item.strip() for item in stock_text.split("\n---\n") if item.strip()]
        duplicate_check = await filter_duplicate_inventory(
            session,
            cipher,
            product_id=product.id,
            raw_items=items,
        )
        session.add_all(
            [
                InventoryItem(
                    product_id=product.id,
                    encrypted_secret=cipher.encrypt(candidate.raw_item),
                    account_fingerprint=candidate.account_fingerprint,
                    cost_amount=cost_amount,
                )
                for candidate in duplicate_check.accepted
            ]
        )
        await session.flush()
        if product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES:
            local_stock = int(
                await session.scalar(
                    select(func.count(InventoryItem.id)).where(
                        InventoryItem.product_id == product.id,
                        InventoryItem.status == "available",
                    )
                )
                or 0
            )
            product.external_stock = local_stock + max(
                0,
                int(product.supplier_available_stock),
            )
        await session.commit()
        if duplicate_check.accepted:
            try:
                await message.delete()
            except Exception:
                pass
        await message.answer(
            f"Đã thêm {len(duplicate_check.accepted)} món sạch vào kho của "
            f"{escape(product.name_vi)} với vốn {format_vnd(cost_amount)}/món; "
            f"bỏ qua {duplicate_check.duplicate_count} món nghi ngờ/trùng. "
            "Chi tiết nằm tại Admin → Nhập kho."
        )

    @router.message(AdminCustomEmojiFilter(set(settings.admin_ids)))
    async def capture_admin_custom_emoji(message: Message) -> None:
        emoji_ids = custom_emoji_ids(message)
        logger.info(
            "Admin custom emoji captured: user_id=%s emoji_ids=%s",
            message.from_user.id if message.from_user else None,
            ",".join(emoji_ids),
        )
        formatted_ids = "\n".join(f"<code>{emoji_id}</code>" for emoji_id in emoji_ids)
        await message.answer(
            "✅ <b>Đã nhận emoji Telegram</b>\n\n"
            f"Custom emoji ID:\n{formatted_ids}\n\n"
            "Bạn có thể gửi ID này để thay emoji thương hiệu trong bot."
        )

    return router
