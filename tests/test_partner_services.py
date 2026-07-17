import asyncio

from aiogram.types import User as TelegramUser
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import ApiClient, Category, InventoryItem, Product, ReferralReward, User
from app.partner_services import ensure_api_client, rotate_api_secret
from app.services import ensure_user, purchase_product
from app.utils import SecretCipher


async def make_database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def telegram_user(user_id: int, name: str) -> TelegramUser:
    return TelegramUser(id=user_id, is_bot=False, first_name=name)


def test_each_user_has_one_rotatable_api_client() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            user = await ensure_user(session, telegram_user(10001, "Partner"))
            first, first_secret = await ensure_api_client(session, user.telegram_id, cipher, 60)
            second, second_secret = await ensure_api_client(session, user.telegram_id, cipher, 60)
            assert first.id == second.id
            assert first_secret is not None
            assert second_secret is None
            assert cipher.decrypt(first.encrypted_secret) == first_secret

            rotated, rotated_secret = await rotate_api_secret(session, user.telegram_id, cipher)
            assert rotated.id == first.id
            assert rotated.api_id == first.api_id
            assert rotated.secret_version == 2
            assert rotated_secret != first_secret
            assert cipher.decrypt(rotated.encrypted_secret) == rotated_secret
            await session.commit()

        async with sessions() as session:
            assert int(await session.scalar(select(func.count(ApiClient.id))) or 0) == 1
        await engine.dispose()

    asyncio.run(scenario())


def test_referrer_receives_five_percent_for_every_completed_batch() -> None:
    async def scenario() -> None:
        engine, sessions = await make_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        async with sessions() as session:
            referrer = await ensure_user(session, telegram_user(20001, "Referrer"))
            await session.commit()
            buyer = await ensure_user(
                session,
                telegram_user(20002, "Buyer"),
                referrer.referral_code,
            )
            buyer.balance = 80_000
            category = Category(name_vi="Tài khoản", name_en="Accounts")
            session.add(category)
            await session.flush()
            product = Product(
                category_id=category.id,
                name_vi="Tài khoản test",
                name_en="Test account",
                price=20_000,
                allow_quantity=True,
                max_quantity=10,
            )
            session.add(product)
            await session.flush()
            session.add_all(
                [
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt(f"account-{index}|password"),
                    )
                    for index in range(4)
                ]
            )
            await session.commit()

        first = await purchase_product(
            sessions,
            buyer.telegram_id,
            product.id,
            cipher,
            quantity=2,
            referral_commission_percent=5,
        )
        second = await purchase_product(
            sessions,
            buyer.telegram_id,
            product.id,
            cipher,
            quantity=2,
            sales_channel="api",
            referral_commission_percent=5,
        )
        assert first.ok is True and second.ok is True

        async with sessions() as session:
            referrer = await session.get(User, 20001)
            buyer = await session.get(User, 20002)
            rewards = list(await session.scalars(select(ReferralReward).order_by(ReferralReward.id)))
            assert referrer is not None and referrer.balance == 4_000
            assert buyer is not None and buyer.balance == 0
            assert len(rewards) == 2
            assert [reward.order_amount for reward in rewards] == [40_000, 40_000]
            assert [reward.commission_amount for reward in rewards] == [2_000, 2_000]
            assert [reward.sales_channel for reward in rewards] == ["telegram", "api"]
        await engine.dispose()

    asyncio.run(scenario())
