import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Category, Product
from app.retired_catalog import retire_discontinued_api_catalog


def test_discontinued_api_catalog_is_archived_without_deleting_rows() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            category = Category(
                name_vi="API CODEX & CLAUDE",
                name_en="CODEX & CLAUDE API",
                position=3,
            )
            session.add(category)
            await session.flush()
            session.add(
                Product(
                    category_id=category.id,
                    name_vi="API CODEX - 50M token",
                    name_en="CODEX API - 50M tokens",
                    price=40_000,
                    external_stock=8,
                )
            )
            await session.commit()

        assert await retire_discontinued_api_catalog(sessions) == 1
        assert await retire_discontinued_api_catalog(sessions) == 0
        async with sessions() as session:
            category = await session.scalar(select(Category))
            product = await session.scalar(select(Product))
            assert category is not None
            assert category.active is False
            assert category.archived_at is not None
            assert product is not None
            assert product.active is False
            assert product.archived_at is not None
            assert product.force_out_of_stock is True
            assert product.external_stock == 0
            assert product.sale_notifications_enabled is False
            assert product.stock_notifications_enabled is False
        await engine.dispose()

    asyncio.run(scenario())


def test_new_haji_codex_product_survives_legacy_catalog_cleanup() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            category = Category(
                name_vi="API CODEX & CLAUDE",
                name_en="CODEX & CLAUDE API",
                position=4,
            )
            session.add(category)
            await session.flush()
            session.add(
                Product(
                    category_id=category.id,
                    name_vi="API Codex 50M Token",
                    name_en="Codex API 50M Tokens",
                    price=50_000,
                    fulfillment_source="haji",
                    supplier_product_id="apicodex_50m_1day",
                )
            )
            await session.commit()

        assert await retire_discontinued_api_catalog(sessions) == 0
        async with sessions() as session:
            category = await session.scalar(select(Category))
            product = await session.scalar(select(Product))
            assert category is not None and category.active is True
            assert category.archived_at is None
            assert product is not None and product.active is True
            assert product.archived_at is None
        await engine.dispose()

    asyncio.run(scenario())


def test_discontinued_source_is_archived_after_category_rename() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            category = Category(name_vi="Kho mã cũ", name_en="Legacy codes")
            session.add(category)
            await session.flush()
            session.add(
                Product(
                    category_id=category.id,
                    name_vi="Gói đã ngừng bán",
                    name_en="Retired package",
                    price=20_000,
                    fulfillment_source="nce",
                )
            )
            await session.commit()

        assert await retire_discontinued_api_catalog(sessions) == 1
        async with sessions() as session:
            category = await session.scalar(select(Category))
            product = await session.scalar(select(Product))
            assert category is not None
            assert category.active is True
            assert product is not None
            assert product.active is False
            assert product.archived_at is not None
        await engine.dispose()

    asyncio.run(scenario())
