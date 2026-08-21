import re
from typing import Any

from aiogram import Bot
from aiogram.methods import TelegramMethod
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup


# Brand emoji are owned by the shop bot; functional emoji use either that pack or
# Telegram's animated topic-icon set. Keeping the IDs here makes all bot surfaces
# consistent without storing media on the VPS.
CHATGPT_EMOJI_ID = "5310259124817134249"
NETFLIX_EMOJI_ID = "5318911503938634641"
GOOGLE_EMOJI_ID = "6319109310144062723"
GEMINI_EMOJI_ID = "6212797771372563847"
BANK_DEPOSIT_EMOJI_ID = "5332455502917949981"
BINANCE_DEPOSIT_EMOJI_ID = "5197434882321567830"
MBBANK_COLUMN_EMOJI_ID = "5197216633558426964"
BINANCE_COLUMN_EMOJI_ID = "5217811903685865303"

EMOJI_IDS = {
    "📣": "5309984423003823246",
    "🔥": "5312241539987020022",
    "💎": "5309958691854754293",
    "💰": "5350452584119279096",
    "🏠": "5312486108309757006",
    "🎬": NETFLIX_EMOJI_ID,
    "📺": "5350513667144163474",
    "🛒": "5431492767249342908",
    "✅": "5237699328843200968",
    "🤖": CHATGPT_EMOJI_ID,
    "⚡": "6318862697416892165",
    "💳": "6318980873442042123",
    "📦": "6318723218353955405",
    "🎁": "6319044653706386388",
    "📲": "6318654408682905127",
    "🧾": "6318744134844686978",
    "🔑": "6318689726198980891",
    "👤": "6318893251814236564",
    "🛠": "6318820340449419096",
    "📘": "6319018596139802510",
    "📤": "6318840406536627334",
    "🔄": "6318624434106149249",
    "🧹": "6318786934193791989",
    "🌐": "6318652621976511109",
    "💬": "6319016448656159334",
    "📋": "6318554396074451012",
    "❌": "6319079833783509795",
    "⏳": "6318783962076423816",
    "⚠": "6319046771125264490",
    "🚨": "6318755280284819767",
    "📢": "6318680985940533918",
    "👛": "6318550444704539172",
    "⌨": "6318937803509997501",
    "✨": "6319079326977367756",
    "🛍": "6318982788997457136",
    "🏷": "6318815710474674600",
    "🧮": "6318978562749638615",
    "📄": "6316407685520564104",
    "🔌": "6318721586266384800",
    "✍": "6318631864399570339",
    "🔒": "6318878146414258271",
    "↩": "6318560645251867024",
    "⬇": "6318930686749189367",
    "🆘": "6319109099690663659",
    "⛔": "6318589958403661297",
    "🔴": "6318565734788112189",
    "🏦": BANK_DEPOSIT_EMOJI_ID,
    "₿": BINANCE_DEPOSIT_EMOJI_ID,
    "☰": "6318554396074451012",
}

_PROTECTED_HTML = re.compile(
    r"(<(?:pre|code|tg-emoji)\b[^>]*>.*?</(?:pre|code|tg-emoji)>)",
    flags=re.IGNORECASE | re.DOTALL,
)
_BUTTON_EMOJI = re.compile(
    "["
    "\U0001F100-\U0001F2FF"
    "\U0001F300-\U0001FAFF"
    "\u2190-\u21FF"
    "\u2300-\u23FF"
    "\u2600-\u27BF"
    "\u2B00-\u2BFF"
    "\uFE0F\u200D"
    "]+"
)


def custom_emoji(fallback: str, emoji_id: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def product_brand_emoji_id(name: str) -> str:
    normalized = " ".join(name.casefold().split())
    if re.search(r"netfli+x", normalized):
        return NETFLIX_EMOJI_ID
    if any(marker in normalized for marker in ("gemini", "veo", "antigravity")):
        return GEMINI_EMOJI_ID
    if "18m" in normalized and any(
        marker in normalized for marker in ("gg", "google", "jio")
    ):
        return GEMINI_EMOJI_ID
    if any(marker in normalized for marker in ("chatgpt", "openai", "gpt", "codex")):
        return CHATGPT_EMOJI_ID
    if any(marker in normalized for marker in ("google", "pixel", "gg pro")):
        return GOOGLE_EMOJI_ID
    return EMOJI_IDS["📦"]


def product_brand_emoji(name: str) -> str:
    emoji_id = product_brand_emoji_id(name)
    fallback = (
        "🎬"
        if emoji_id == NETFLIX_EMOJI_ID
        else "✨"
        if emoji_id == GEMINI_EMOJI_ID
        else "🔎"
        if emoji_id == GOOGLE_EMOJI_ID
        else "🤖"
        if emoji_id == CHATGPT_EMOJI_ID
        else "📦"
    )
    return custom_emoji(fallback, emoji_id)


def animate_html(text: str) -> str:
    """Replace supported emoji outside code blocks with Telegram custom emoji."""
    if not text:
        return text
    parts = _PROTECTED_HTML.split(text)
    for index in range(0, len(parts), 2):
        part = parts[index]
        for fallback, emoji_id in EMOJI_IDS.items():
            part = re.sub(
                re.escape(fallback) + "\ufe0f?",
                lambda match, current_id=emoji_id: custom_emoji(
                    match.group(0), current_id
                ),
                part,
            )
        parts[index] = part
    return "".join(parts)


def button_emoji_id(text: str) -> str | None:
    normalized = " ".join(text.casefold().split())
    if "mbbank" in normalized:
        return MBBANK_COLUMN_EMOJI_ID
    if "binance" in normalized:
        return BINANCE_COLUMN_EMOJI_ID
    if "🇻🇳" in text or "🇺🇸" in text:
        return EMOJI_IDS["🌐"]
    if any(
        marker in normalized
        for marker in (
            "netflix",
            "netfliix",
            "chatgpt",
            "openai",
            "gpt",
            "codex",
            "gemini",
            "veo",
            "antigravity",
            "pixel",
            "gg pro",
        )
    ):
        return product_brand_emoji_id(text)
    for fallback, emoji_id in EMOJI_IDS.items():
        if fallback in text:
            return emoji_id
    keyword_icons = (
        (("mua nhanh", "quick buy", "buy now"), EMOJI_IDS["🛒"]),
        (("nạp tiền", "deposit", "payment", "thanh toán"), EMOJI_IDS["💳"]),
        (("lấy code", "my codes", "code"), EMOJI_IDS["🔑"]),
        (("mặt hàng", "products", "stock"), EMOJI_IDS["📦"]),
        (("thuê số", "rent sms", "rent now"), EMOJI_IDS["📲"]),
        (("đơn mua", "orders", "history"), EMOJI_IDS["🧾"]),
        (("hồ sơ", "profile"), EMOJI_IDS["👤"]),
        (("api đấu kho", "warehouse api"), EMOJI_IDS["🌐"]),
        (("giới thiệu", "referral", "coupon"), EMOJI_IDS["🎁"]),
        (("hỗ trợ", "support"), EMOJI_IDS["💬"]),
        (("xóa hội thoại", "clear chat"), EMOJI_IDS["🧹"]),
        (("ngôn ngữ", "language"), EMOJI_IDS["🌐"]),
        (("menu",), EMOJI_IDS["📋"]),
        (("quay lại", "back"), EMOJI_IDS["🏠"]),
    )
    for markers, emoji_id in keyword_icons:
        if any(marker in normalized for marker in markers):
            return emoji_id
    return None


def reply_button_emoji_id(text: str) -> str | None:
    normalized = " ".join(_strip_animated_prefix(text).casefold().split())
    if normalized == "menu":
        return EMOJI_IDS["🏠"]
    if normalized in {"mua nhanh", "quick buy"}:
        return EMOJI_IDS["🛒"]
    if normalized in {"nạp tiền", "deposit"}:
        return EMOJI_IDS["💳"]
    return None


def _strip_animated_prefix(text: str) -> str:
    # Button icons are rendered separately by Telegram. Remove any legacy emoji,
    # including flags embedded later in the label, so only one icon is visible.
    cleaned = _BUTTON_EMOJI.sub("", text)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def decorate_keyboard(markup: Any) -> None:
    if not isinstance(markup, (InlineKeyboardMarkup, ReplyKeyboardMarkup)):
        return
    is_reply_keyboard = isinstance(markup, ReplyKeyboardMarkup)
    for row in markup.inline_keyboard if not is_reply_keyboard else markup.keyboard:
        for button in row:
            if button.icon_custom_emoji_id:
                continue
            emoji_id = (
                reply_button_emoji_id(button.text)
                if is_reply_keyboard
                else button_emoji_id(button.text)
            )
            emoji_id = emoji_id or button_emoji_id(button.text)
            if emoji_id:
                button.icon_custom_emoji_id = emoji_id
                button.text = _strip_animated_prefix(button.text)


def prepare_telegram_method(method: TelegramMethod[Any]) -> None:
    if hasattr(method, "parse_mode"):
        for field in ("text", "caption"):
            value = getattr(method, field, None)
            if isinstance(value, str):
                setattr(method, field, animate_html(value))
    decorate_keyboard(getattr(method, "reply_markup", None))


class AnimatedEmojiBot(Bot):
    async def __call__(
        self,
        method: TelegramMethod[Any],
        request_timeout: int | None = None,
    ) -> Any:
        prepare_telegram_method(method)
        return await super().__call__(method, request_timeout=request_timeout)
