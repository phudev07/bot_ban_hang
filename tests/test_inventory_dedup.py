import asyncio

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.inventory_dedup import backfill_inventory_fingerprints
from app.models import Category, InventoryItem, Product
from app.utils import SecretCipher


def test_old_inventory_items_receive_fingerprints(tmp_path) -> None:
    async def scenario() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{(tmp_path / 'inventory-dedup.db').as_posix()}"
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            category = Category(name_vi="Test", name_en="Test")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Test account",
                name_en="Test account",
                price=10_000,
            )
            session.add(product)
            await session.flush()
            session.add(
                InventoryItem(
                    product_id=product.id,
                    encrypted_secret=cipher.encrypt("Old@Example.com|password"),
                )
            )
            await session.commit()

        assert await backfill_inventory_fingerprints(sessions, cipher) == 1
        async with sessions() as session:
            item = await session.scalar(select(InventoryItem))
            assert item is not None
            assert item.account_fingerprint == cipher.inventory_fingerprint(
                "old@example.com|another-password"
            )
        await engine.dispose()

    asyncio.run(scenario())
