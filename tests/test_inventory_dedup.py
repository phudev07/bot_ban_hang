import asyncio

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.inventory_dedup import (
    backfill_historical_duplicate_alerts,
    backfill_inventory_fingerprints,
)
from app.models import Category, InventoryDuplicateAlert, InventoryItem, Product
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
            session.add(
                InventoryItem(
                    product_id=product.id,
                    encrypted_secret=cipher.encrypt("old@example.com|other-password"),
                )
            )
            await session.commit()

        assert await backfill_inventory_fingerprints(sessions, cipher) == 2
        assert await backfill_historical_duplicate_alerts(sessions, cipher) == 1
        assert await backfill_historical_duplicate_alerts(sessions, cipher) == 0
        async with sessions() as session:
            items = list(
                await session.scalars(select(InventoryItem).order_by(InventoryItem.id))
            )
            assert len(items) == 2
            assert items[0].account_fingerprint == cipher.inventory_fingerprint(
                "old@example.com|another-password"
            )
            assert items[1].account_fingerprint == items[0].account_fingerprint
            alert = await session.scalar(select(InventoryDuplicateAlert))
            assert alert is not None
            assert alert.reason == "historical_duplicate"
            assert alert.existing_inventory_item_id == items[0].id
        await engine.dispose()

    asyncio.run(scenario())


def test_supplier_contact_instructions_do_not_create_duplicate_alerts(tmp_path) -> None:
    async def scenario() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{(tmp_path / 'inventory-contact.db').as_posix()}"
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
                name_vi="Supplier item",
                name_en="Supplier item",
                price=10_000,
            )
            session.add(product)
            await session.flush()
            for _ in range(2):
                session.add(
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt(
                            "Liên hệ @seller có hàng ngay sau 1p"
                        ),
                    )
                )
            await session.commit()

        assert await backfill_inventory_fingerprints(sessions, cipher) == 0
        assert await backfill_historical_duplicate_alerts(sessions, cipher) == 0
        async with sessions() as session:
            assert await session.scalar(select(InventoryDuplicateAlert)) is None
        await engine.dispose()

    asyncio.run(scenario())
