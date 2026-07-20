import logging
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, LinkPreviewOptions, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import TutorialMedia


logger = logging.getLogger(__name__)
PIXEL_PRODUCT_ID = "cdk_pixel"
TUTORIAL_DIR = Path(__file__).parent / "assets" / "tutorials"


@dataclass(frozen=True)
class TutorialVideo:
    slug: str
    filename: str
    caption_vi: str
    caption_en: str


PIXEL_TUTORIALS = (
    TutorialVideo(
        slug="pixel-google-2fa",
        filename="lay_2fa_gmail.mp4",
        caption_vi="🎬 <b>Video 1: Cách lấy mã 2FA của tài khoản Google</b>",
        caption_en="🎬 <b>Video 1: How to get the Google account 2FA code</b>",
    ),
    TutorialVideo(
        slug="pixel-activate-one-year",
        filename="kich_hoat_pixel.mp4",
        caption_vi="🎬 <b>Video 2: Cách kích hoạt Google Pixel 1 năm</b>",
        caption_en="🎬 <b>Video 2: How to activate Google Pixel for one year</b>",
    ),
)


def pixel_instruction_text(language: str) -> str:
    if language == "en":
        return (
            "📘 <b>GOOGLE PIXEL 1 YEAR ACTIVATION GUIDE</b>\n\n"
            "1. Watch the first video to get the Google account 2FA code.\n"
            "2. Copy the key delivered in your order.\n"
            "3. Open <a href=\"https://renewgpt.online/\">renewgpt.online</a>.\n"
            "4. Paste the key, then enter the account email, password and 2FA code.\n"
            "5. Confirm the upgrade and follow the Pixel activation video."
        )
    return (
        "📘 <b>HƯỚNG DẪN KÍCH HOẠT GOOGLE PIXEL 1 NĂM</b>\n\n"
        "1. Xem video đầu tiên để biết cách lấy mã 2FA của tài khoản Google.\n"
        "2. Sao chép key vừa nhận trong đơn hàng.\n"
        "3. Truy cập <a href=\"https://renewgpt.online/\">renewgpt.online</a>.\n"
        "4. Dán key, sau đó nhập Email, Password và mã 2FA của tài khoản.\n"
        "5. Xác nhận nâng cấp và làm theo video kích hoạt Pixel bên dưới."
    )


async def _cached_file_id(
    session_factory: async_sessionmaker[AsyncSession],
    slug: str,
) -> str | None:
    async with session_factory() as session:
        media = await session.get(TutorialMedia, slug)
        return media.telegram_file_id if media is not None else None


async def _save_file_id(
    session_factory: async_sessionmaker[AsyncSession],
    slug: str,
    message: Message,
) -> None:
    if message.video is None:
        return
    async with session_factory() as session:
        media = await session.get(TutorialMedia, slug)
        if media is None:
            media = TutorialMedia(slug=slug)
            session.add(media)
        media.telegram_file_id = message.video.file_id
        media.telegram_file_unique_id = message.video.file_unique_id
        await session.commit()


async def _send_tutorial_video(
    bot: Bot,
    chat_id: int,
    tutorial: TutorialVideo,
    language: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    caption = tutorial.caption_en if language == "en" else tutorial.caption_vi
    cached_id = await _cached_file_id(session_factory, tutorial.slug)
    video = cached_id or FSInputFile(TUTORIAL_DIR / tutorial.filename)
    try:
        sent = await bot.send_video(
            chat_id,
            video,
            caption=caption,
            supports_streaming=True,
        )
    except TelegramBadRequest:
        if not cached_id:
            raise
        sent = await bot.send_video(
            chat_id,
            FSInputFile(TUTORIAL_DIR / tutorial.filename),
            caption=caption,
            supports_streaming=True,
        )
    await _save_file_id(session_factory, tutorial.slug, sent)


async def send_purchase_tutorials(
    bot: Bot,
    chat_id: int,
    supplier_product_id: str | None,
    language: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    if supplier_product_id != PIXEL_PRODUCT_ID:
        return False
    try:
        await bot.send_message(
            chat_id,
            pixel_instruction_text(language),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        for tutorial in PIXEL_TUTORIALS:
            await _send_tutorial_video(
                bot,
                chat_id,
                tutorial,
                language,
                session_factory,
            )
    except Exception:
        logger.exception("Could not send Pixel tutorials to user %s", chat_id)
        return False
    return True
