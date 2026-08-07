import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove

from app.handlers import (
    coupon_error_message,
    edit_or_send_text,
    home_text,
    send_home_with_navigation,
)


def test_coupon_errors_explain_the_exact_reason() -> None:
    assert coupon_error_message("coupon_not_found", "vi") == "Mã giảm giá không tồn tại."
    assert (
        coupon_error_message("coupon_exhausted", "vi")
        == "Mã giảm giá đã hết lượt sử dụng."
    )
    assert (
        coupon_error_message("coupon_already_used", "vi")
        == "Bạn đã sử dụng mã giảm giá này rồi."
    )


def test_edit_or_send_text_edits_normal_messages() -> None:
    async def scenario() -> None:
        message = AsyncMock()
        message.text = "Current menu"

        await edit_or_send_text(message, "Next menu", reply_markup="keyboard")

        message.edit_text.assert_awaited_once_with(
            "Next menu",
            reply_markup="keyboard",
        )
        message.answer.assert_not_awaited()

    asyncio.run(scenario())


def test_edit_or_send_text_sends_new_message_for_media() -> None:
    async def scenario() -> None:
        message = AsyncMock()
        message.text = None

        await edit_or_send_text(message, "Product categories", reply_markup="keyboard")

        message.answer.assert_awaited_once_with(
            "Product categories",
            reply_markup="keyboard",
        )
        message.edit_text.assert_not_awaited()

    asyncio.run(scenario())


def test_start_home_keeps_quick_actions_and_shows_main_menu() -> None:
    async def scenario() -> None:
        message = AsyncMock()
        user = SimpleNamespace(
            language="vi",
            full_name="Test User",
            username="tester",
            telegram_id=123,
            balance=50_000,
        )
        settings = SimpleNamespace(
            support_username="support",
            community_group_url="",
        )

        await send_home_with_navigation(
            message,
            user,  # type: ignore[arg-type]
            settings,  # type: ignore[arg-type]
            sms_enabled=True,
        )

        assert message.answer.await_count == 3
        remove_call, quick_call, menu_call = message.answer.await_args_list
        assert isinstance(remove_call.kwargs["reply_markup"], ReplyKeyboardRemove)
        assert isinstance(quick_call.kwargs["reply_markup"], ReplyKeyboardMarkup)
        assert isinstance(menu_call.kwargs["reply_markup"], InlineKeyboardMarkup)
        message.answer.return_value.delete.assert_awaited_once()
        menu_callbacks = {
            button.callback_data
            for row in menu_call.kwargs["reply_markup"].inline_keyboard
            for button in row
        }
        assert "menu:quick" in menu_callbacks
        assert "menu:deposit" in menu_callbacks
        assert "menu:sms" in menu_callbacks

    asyncio.run(scenario())


def test_home_text_uses_scannable_animated_icon_sections() -> None:
    user = SimpleNamespace(
        language="vi",
        full_name="Test User",
        username="tester",
        telegram_id=123,
        balance=50_000,
    )
    settings = SimpleNamespace(
        support_username="support",
        community_group_url="https://t.me/example",
    )

    text = home_text(user, settings)  # type: ignore[arg-type]

    assert "🧾 ID:" in text
    assert "👤 Username:" in text
    assert "👛 Số dư khả dụng:" in text
    assert "⚡ <b>Truy cập nhanh</b>" in text
    assert "⌨️ Ba nút dưới ô chat" in text
