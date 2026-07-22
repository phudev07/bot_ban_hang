import asyncio
from unittest.mock import AsyncMock

from app.handlers import coupon_error_message, edit_or_send_text


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
