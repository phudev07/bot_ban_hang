from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.custom_emoji import (
    BANK_DEPOSIT_EMOJI_ID,
    BINANCE_COLUMN_EMOJI_ID,
    BINANCE_DEPOSIT_EMOJI_ID,
    MBBANK_COLUMN_EMOJI_ID,
    CHATGPT_EMOJI_ID,
    CLAUDE_EMOJI_ID,
    EMOJI_IDS,
    GEMINI_EMOJI_ID,
    NETFLIX_EMOJI_ID,
    animate_html,
    button_emoji_id,
    prepare_telegram_method,
    product_brand_emoji_id,
)
from app.keyboards import quick_access_keyboard


def test_product_brand_emoji_uses_exact_service_logo() -> None:
    assert product_brand_emoji_id("ChatGPT Plus") == CHATGPT_EMOJI_ID
    assert product_brand_emoji_id("Claude Team Standard 1 tháng") == CLAUDE_EMOJI_ID
    assert product_brand_emoji_id("Netflix 4K Premium") == NETFLIX_EMOJI_ID
    assert product_brand_emoji_id("Netfliix 4K HD TK RIÊNG BHF") == NETFLIX_EMOJI_ID
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

    assert rendered.startswith(
        f'<tg-emoji emoji-id="{EMOJI_IDS["🛠"]}">🛠️</tg-emoji>'
    )
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


def test_prepare_method_removes_unmapped_legacy_button_emoji() -> None:
    button = InlineKeyboardButton(text="🔌 API đấu kho", callback_data="menu:api")
    method = SendMessage(
        chat_id=1,
        text="Menu",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]),
    )

    prepare_telegram_method(method)

    assert button.text == "API đấu kho"
    assert button.icon_custom_emoji_id == EMOJI_IDS["🔌"]


def test_prepare_method_replaces_all_flags_with_one_language_icon() -> None:
    button = InlineKeyboardButton(text="🇻🇳 VN / 🇺🇸 US", callback_data="language")
    method = SendMessage(
        chat_id=1,
        text="Menu",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]),
    )

    prepare_telegram_method(method)

    assert button.text == "VN / US"
    assert button.icon_custom_emoji_id == EMOJI_IDS["🌐"]


def test_animate_html_covers_customer_information_icons() -> None:
    rendered = animate_html("🏷️ Mã giảm giá\n🧮 Số lượng\n👛 Số dư\n⏳ Chờ xử lý")

    for emoji in ("🏷", "🧮", "👛", "⏳"):
        assert f'emoji-id="{EMOJI_IDS[emoji]}"' in rendered


def test_animate_html_uses_valid_coin_fallback_for_binance() -> None:
    rendered = animate_html("🪙 Binance Pay")

    assert rendered.startswith(
        f'<tg-emoji emoji-id="{BINANCE_DEPOSIT_EMOJI_ID}">🪙</tg-emoji>'
    )
    # U+20BF must never be emitted as custom-emoji fallback content.
    assert "₿" not in rendered


def test_quick_access_buttons_use_one_animated_icon_each() -> None:
    method = SendMessage(
        chat_id=1,
        text="Menu",
        reply_markup=quick_access_keyboard("vi"),
    )

    prepare_telegram_method(method)

    buttons = [button for row in method.reply_markup.keyboard for button in row]
    assert [button.text for button in buttons] == ["Menu", "Mua nhanh", "Nạp tiền"]
    assert [button.icon_custom_emoji_id for button in buttons] == [
        EMOJI_IDS["🏠"],
        EMOJI_IDS["🛒"],
        EMOJI_IDS["💳"],
    ]


def test_deposit_provider_emojis_are_distinct() -> None:
    assert button_emoji_id("🏦 50.000đ") == BANK_DEPOSIT_EMOJI_ID
    assert button_emoji_id("🪙 $1") == BINANCE_DEPOSIT_EMOJI_ID
    assert button_emoji_id("MBBank") == MBBANK_COLUMN_EMOJI_ID
    assert button_emoji_id("Binance") == BINANCE_COLUMN_EMOJI_ID
