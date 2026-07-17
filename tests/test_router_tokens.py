import asyncio
from types import SimpleNamespace

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import (
    Category,
    InventoryItem,
    Order,
    Product,
    RouterCapacityState,
    RouterTokenPurchase,
    User,
)
from app.router_tokens import (
    RouterCapacityUsage,
    RouterTokenClient,
    claim_router_capacity,
    create_wallet_router_purchase,
    notify_router_token_purchase,
    refresh_router_capacity,
    token_quota_for_amount,
)
from app.utils import SecretCipher


def router_settings() -> SimpleNamespace:
    return SimpleNamespace(
        router_min_purchase=10_000,
        router_tokens_per_vnd=1_000,
        router_capacity_tokens=0,
        router_capacity_reserve_tokens=10_000_000,
        router_capacity_sync_seconds=60,
    )


async def create_database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_token_quota_uses_combined_rate() -> None:
    assert token_quota_for_amount(10_000, router_settings()) == 10_000_000
    assert token_quota_for_amount(25_000, router_settings()) == 25_000_000


def test_wallet_router_purchase_deducts_once_and_creates_outbox() -> None:
    async def scenario() -> None:
        engine, session_factory = await create_database()
        try:
            async with session_factory() as session:
                category = Category(name_vi="LLM", name_en="LLM", position=1)
                session.add(category)
                await session.flush()
                product = Product(
                    category_id=category.id,
                    name_vi="GPT token",
                    name_en="GPT token",
                    price=20_000,
                    product_type="token",
                    fulfillment_source="9router",
                    supplier_price=2_000,
                )
                user = User(
                    telegram_id=123,
                    full_name="Tester",
                    language="vi",
                    balance=50_000,
                )
                session.add_all([product, user])
                await session.commit()

            result = await create_wallet_router_purchase(
                session_factory,
                router_settings(),
                123,
                product.id,
                25_000,
            )
            assert result.ok is True
            assert result.paid_amount == 25_000
            assert result.token_quota == 25_000_000

            async with session_factory() as session:
                stored_user = await session.get(User, 123)
                purchase = await session.scalar(select(RouterTokenPurchase))
                assert stored_user.balance == 25_000
                assert purchase.status == "pending"
                assert purchase.token_quota == 25_000_000
                assert purchase.cost_amount == 5_000
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_insufficient_wallet_does_not_create_purchase() -> None:
    async def scenario() -> None:
        engine, session_factory = await create_database()
        try:
            async with session_factory() as session:
                category = Category(name_vi="LLM", name_en="LLM", position=1)
                session.add(category)
                await session.flush()
                product = Product(
                    category_id=category.id,
                    name_vi="GPT token",
                    name_en="GPT token",
                    price=10_000,
                    product_type="token",
                    fulfillment_source="9router",
                )
                user = User(
                    telegram_id=456,
                    full_name="Tester",
                    language="vi",
                    balance=5_000,
                )
                session.add_all([product, user])
                await session.commit()

            result = await create_wallet_router_purchase(
                session_factory,
                router_settings(),
                456,
                product.id,
                10_000,
            )
            assert result.ok is False
            assert result.message == "insufficient"

            async with session_factory() as session:
                stored_user = await session.get(User, 456)
                purchase = await session.scalar(select(RouterTokenPurchase))
                assert stored_user.balance == 5_000
                assert purchase is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_router_delivery_claim_prevents_duplicate_messages() -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.messages: list[tuple[tuple[object, ...], dict[str, object]]] = []

        async def send_message(self, *args, **kwargs) -> None:
            await asyncio.sleep(0.01)
            self.messages.append((args, kwargs))

    async def scenario() -> None:
        engine, session_factory = await create_database()
        cipher = SecretCipher(Fernet.generate_key().decode())
        bot = FakeBot()
        try:
            async with session_factory() as session:
                category = Category(name_vi="LLM", name_en="LLM", position=1)
                session.add(category)
                await session.flush()
                product = Product(
                    category_id=category.id,
                    name_vi="GPT token",
                    name_en="GPT token",
                    price=10_000,
                    product_type="token",
                    fulfillment_source="9router",
                )
                user = User(telegram_id=999, full_name="Delivery Tester", language="vi")
                session.add_all([product, user])
                await session.flush()
                item = InventoryItem(
                    product_id=product.id,
                    encrypted_secret=cipher.encrypt("sk-test-delivery"),
                    status="sold",
                )
                session.add(item)
                await session.flush()
                order = Order(
                    user_id=user.telegram_id,
                    product_id=product.id,
                    inventory_item_id=item.id,
                    amount=10_000,
                    status="completed",
                )
                session.add(order)
                await session.flush()
                purchase = RouterTokenPurchase(
                    shop_order_id="RT-NOTIFY-ONCE",
                    user_id=user.telegram_id,
                    product_id=product.id,
                    order_id=order.id,
                    source="wallet",
                    face_amount=10_000,
                    paid_amount=10_000,
                    token_quota=10_000_000,
                    encrypted_key=item.encrypted_secret,
                    status="fulfilled",
                )
                session.add(purchase)
                await session.commit()

            results = await asyncio.gather(
                notify_router_token_purchase(
                    session_factory,
                    bot,  # type: ignore[arg-type]
                    cipher,
                    "https://router.example.com/v1",
                    purchase.id,
                ),
                notify_router_token_purchase(
                    session_factory,
                    bot,  # type: ignore[arg-type]
                    cipher,
                    "https://router.example.com/v1",
                    purchase.id,
                ),
            )
            assert sorted(results) == [False, True]
            assert len(bot.messages) == 3
            texts = [str(args[1]) for args, _kwargs in bot.messages]
            assert "https://router.example.com/v1" in texts[0]
            assert '<pre><code class="language-toml">' in texts[1]
            assert 'base_url = &quot;https://router.example.com/v1&quot;' in texts[1]
            assert texts[1].count('model = &quot;cx/gpt-5.6-sol&quot;') == 2
            assert '<pre><code class="language-json">' in texts[2]
            assert '&quot;OPENAI_API_KEY&quot;: &quot;sk-test-delivery&quot;' in texts[2]
            assert all(kwargs.get("reply_markup") is not None for _args, kwargs in bot.messages)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_router_capacity_aggregates_usage_and_blocks_overselling() -> None:
    class FakeClient:
        async def capacity(self) -> RouterCapacityUsage:
            return RouterCapacityUsage(
                key_count=1,
                active_keys=1,
                exhausted_keys=0,
                token_quota=10_000_000,
                tokens_used=4_000_000,
                reserved_tokens=0,
                remaining_tokens=6_000_000,
                available_tokens=6_000_000,
            )

    async def scenario() -> None:
        engine, session_factory = await create_database()
        settings = SimpleNamespace(
            router_min_purchase=10_000,
            router_tokens_per_vnd=1_000,
            router_capacity_tokens=30_000_000,
            router_capacity_reserve_tokens=5_000_000,
            router_capacity_sync_seconds=60,
        )
        try:
            async with session_factory() as session:
                category = Category(name_vi="LLM", name_en="LLM", position=1)
                session.add(category)
                await session.flush()
                product = Product(
                    category_id=category.id,
                    name_vi="GPT token",
                    name_en="GPT token",
                    price=10_000,
                    product_type="token",
                    fulfillment_source="9router",
                )
                user = User(telegram_id=321, full_name="Capacity Tester", language="vi")
                session.add_all([product, user])
                await session.flush()
                session.add_all(
                    [
                        RouterTokenPurchase(
                            shop_order_id="RT-FULFILLED",
                            user_id=user.telegram_id,
                            product_id=product.id,
                            source="wallet",
                            face_amount=10_000,
                            paid_amount=10_000,
                            token_quota=10_000_000,
                            status="fulfilled",
                        ),
                        RouterTokenPurchase(
                            shop_order_id="RT-PENDING",
                            user_id=user.telegram_id,
                            product_id=product.id,
                            source="wallet",
                            face_amount=5_000,
                            paid_amount=5_000,
                            token_quota=5_000_000,
                            status="pending",
                        ),
                    ]
                )
                await session.commit()

            snapshot = await refresh_router_capacity(
                session_factory,
                FakeClient(),  # type: ignore[arg-type]
                settings,  # type: ignore[arg-type]
            )
            assert snapshot.issued_quota_tokens == 15_000_000
            assert snapshot.used_tokens == 4_000_000
            assert snapshot.outstanding_tokens == 11_000_000
            assert snapshot.available_tokens == 19_000_000
            assert snapshot.sellable_tokens == 14_000_000
            assert snapshot.status == "healthy"

            restocked_settings = SimpleNamespace(**vars(settings))
            restocked_settings.router_capacity_tokens = 60_000_000
            restocked = await refresh_router_capacity(
                session_factory,
                FakeClient(),  # type: ignore[arg-type]
                restocked_settings,  # type: ignore[arg-type]
            )
            assert restocked.available_tokens == 49_000_000
            assert restocked.sellable_tokens == 44_000_000

            await refresh_router_capacity(
                session_factory,
                FakeClient(),  # type: ignore[arg-type]
                settings,  # type: ignore[arg-type]
            )

            async with session_factory() as session:
                async with session.begin():
                    blocked = await claim_router_capacity(
                        session,
                        requested_tokens=15_000_000,
                        total_capacity_tokens=30_000_000,
                        reserve_tokens=5_000_000,
                        sync_seconds=60,
                        claim=True,
                    )
                    assert blocked is False
                    claimed = await claim_router_capacity(
                        session,
                        requested_tokens=14_000_000,
                        total_capacity_tokens=30_000_000,
                        reserve_tokens=5_000_000,
                        sync_seconds=60,
                        claim=True,
                    )
                    assert claimed is True

            async with session_factory() as session:
                state = await session.get(RouterCapacityState, 1)
                assert state.outstanding_tokens == 25_000_000
                assert state.available_tokens == 5_000_000
                assert state.status == "low"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_router_capacity_client_uses_single_signed_aggregate_request() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/internal/shop/capacity"
            assert request.headers.get("x-shop-timestamp")
            assert request.headers.get("x-shop-signature")
            return httpx.Response(
                200,
                json={
                    "keyCount": 3,
                    "activeKeys": 2,
                    "exhaustedKeys": 1,
                    "tokenQuota": 30_000_000,
                    "tokensUsed": 7_000_000,
                    "reservedTokens": 1_000,
                    "remainingTokens": 23_000_000,
                    "availableTokens": 22_999_000,
                },
            )

        settings = SimpleNamespace(
            router_base_url="https://router.test",
            router_hmac_secret=SimpleNamespace(get_secret_value=lambda: "test-secret"),
            router_public_api_url="https://token.test/v1",
            token_usage_url="https://token.test/token-usage",
            router_allowed_models=("*/gpt-*",),
            router_timeout_seconds=5,
        )
        client = RouterTokenClient(settings)  # type: ignore[arg-type]
        await client.client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            capacity = await client.capacity()
            assert capacity.key_count == 3
            assert capacity.active_keys == 2
            assert capacity.tokens_used == 7_000_000
            assert capacity.available_tokens == 22_999_000
        finally:
            await client.close()

    asyncio.run(scenario())
