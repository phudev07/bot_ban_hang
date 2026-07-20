import asyncio
from pathlib import Path
from types import SimpleNamespace

from aiogram.types import FSInputFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import TutorialMedia
from app.product_tutorials import (
    PIXEL_PRODUCT_ID,
    TUTORIAL_DIR,
    send_purchase_tutorials,
)


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.videos: list[object] = []

    async def send_message(self, chat_id: int, text: str, **_kwargs):
        self.messages.append((chat_id, text))
        return SimpleNamespace()

    async def send_video(self, chat_id: int, video: object, **_kwargs):
        self.videos.append(video)
        suffix = Path(video.path).stem if isinstance(video, FSInputFile) else str(video)
        return SimpleNamespace(
            video=SimpleNamespace(
                file_id=f"cached-{suffix}",
                file_unique_id=f"unique-{suffix}",
            )
        )


def test_pixel_tutorials_upload_once_then_reuse_telegram_file_ids() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        bot = FakeBot()

        assert await send_purchase_tutorials(
            bot, 123, PIXEL_PRODUCT_ID, "vi", sessions  # type: ignore[arg-type]
        )
        assert len(bot.messages) == 1
        assert "renewgpt.online" in bot.messages[0][1]
        assert all(isinstance(video, FSInputFile) for video in bot.videos)

        async with sessions() as session:
            cached = list(await session.scalars(select(TutorialMedia)))
            assert len(cached) == 2
            assert all(media.telegram_file_id for media in cached)

        assert await send_purchase_tutorials(
            bot, 123, PIXEL_PRODUCT_ID, "vi", sessions  # type: ignore[arg-type]
        )
        assert len(bot.messages) == 2
        assert all(isinstance(video, str) for video in bot.videos[2:])
        await engine.dispose()

    asyncio.run(scenario())


def test_tutorial_assets_exist_and_other_products_stay_quiet() -> None:
    assert (TUTORIAL_DIR / "lay_2fa_gmail.mp4").stat().st_size > 0
    assert (TUTORIAL_DIR / "kich_hoat_pixel.mp4").stat().st_size > 0

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        bot = FakeBot()
        assert not await send_purchase_tutorials(
            bot, 123, "cdk_ggpro_18m", "vi", sessions  # type: ignore[arg-type]
        )
        assert bot.messages == []
        assert bot.videos == []
        await engine.dispose()

    asyncio.run(scenario())
