from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.custom_emoji import (
    CHATGPT_EMOJI_ID,
    GEMINI_EMOJI_ID,
    NETFLIX_EMOJI_ID,
    animate_html,
    prepare_telegram_method,
    product_brand_emoji_id,
)


def test_product_brand_emoji_uses_exact_service_logo() -> None:
    assert product_brand_emoji_id("ChatGPT Plus") == CHATGPT_EMOJI_ID
    assert product_brand_emoji_id("Netflix 4K Premium") == NETFLIX_EMOJI_ID
    assert product_brand_emoji_id("Link GG Pro Jio 18M") == GEMINI_EMOJI_ID


def test_animate_html_preserves_code_and_existing_custom_emoji() -> None:
    existing = '<tg-emoji emoji-id="123">🔥</tg-emoji>'
    text = f"🔥 Sale <code>key🔥</code> {existing}"
    rendered = animate_html(text)

    assert rendered.startswith('<tg-emoji emoji-id="5312241539987020022">🔥</tg-emoji>')
    assert "<code>key🔥</code>" in rendered
    assert rendered.endswith(existing)


def test_animate_html_keeps_variation_selector_inside_custom_emoji() -> None:
    rendered = animate_html("🛠️ Bảo trì")

    assert rendered.startswith('<tg-emoji emoji-id="6318738796200338411">🛠️</tg-emoji>')
    assert "</tg-emoji>️" not in rendered


def test_prepare_method_animates_text_and_decorates_product_button() -> None:
    button = InlineKeyboardButton(text="ChatGPT Plus · 40.000đ", callback_data="prod:1")
    method = SendMessage(
        chat_id=1,
        text="✅ <b>Thành công</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]),
    )

    prepare_telegram_method(method)

    assert '<tg-emoji emoji-id="5237699328843200968">✅</tg-emoji>' in method.text
    assert button.icon_custom_emoji_id == CHATGPT_EMOJI_ID
