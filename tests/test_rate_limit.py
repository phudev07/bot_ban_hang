import asyncio
from datetime import UTC, datetime

from aiogram.types import CallbackQuery, Chat, Message, Update, User as TelegramUser
from cryptography.fernet import Fernet
from redis.exceptions import RedisError

from app.config import Settings
from app.rate_limit import (
    BotSpamProtectionMiddleware,
    FixedWindowRateLimiter,
    RateLimitRule,
    bot_rate_rules,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, int | str] = {}

    async def incr(self, key: str) -> int:
        value = int(self.values.get(key, 0)) + 1
        self.values[key] = value
        return value

    async def expire(self, _key: str, _seconds: int) -> bool:
        return True

    async def set(self, key: str, value: str, **kwargs):
        if kwargs.get("nx") and key in self.values:
            return False
        self.values[key] = value
        return True


class BrokenRedis(FakeRedis):
    async def incr(self, _key: str) -> int:
        raise RedisError("offline")


def settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "bot_token": "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        "inventory_encryption_key": Fernet.generate_key().decode(),
        "sepay_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def telegram_user(user_id: int = 1001) -> TelegramUser:
    return TelegramUser(id=user_id, is_bot=False, first_name="Rate test")


def callback_update(data: str, update_id: int = 1) -> Update:
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=f"callback-{update_id}",
            from_user=telegram_user(),
            chat_instance="rate-test",
            data=data,
        ),
    )


def message(text: str) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=1001, type="private"),
        from_user=telegram_user(),
        text=text,
    )


def test_fixed_window_limit_and_redis_failure_are_safe() -> None:
    async def scenario() -> None:
        now = [100.0]
        limiter = FixedWindowRateLimiter(FakeRedis(), "test", clock=lambda: now[0])
        rule = RateLimitRule("minute", 2, 60)
        assert (await limiter.hit("user:1", (rule,))).allowed is True
        assert (await limiter.hit("user:1", (rule,))).allowed is True
        blocked = await limiter.hit("user:1", (rule,))
        assert blocked.allowed is False and blocked.retry_after == 20

        now[0] = 120.0
        assert (await limiter.hit("user:1", (rule,))).allowed is True
        broken = FixedWindowRateLimiter(BrokenRedis(), "broken", clock=lambda: now[0])
        assert (await broken.hit("user:1", (rule,))).allowed is True

    asyncio.run(scenario())


def test_sensitive_bot_actions_receive_stricter_limits() -> None:
    config = settings()
    deposit_rules = bot_rate_rules(callback_update("deposit:10000").callback_query, config)
    purchase_rules = bot_rate_rules(callback_update("buy:1:1").callback_query, config)
    rotate_rules = bot_rate_rules(
        callback_update("warehouse-api:rotate-confirm").callback_query,
        config,
    )
    command_rules = bot_rate_rules(message("/donchat"), config)

    assert any(rule.name == "deposit" and rule.window_seconds == 300 for rule in deposit_rules)
    assert any(rule.name == "purchase" for rule in purchase_rules)
    assert any(rule.name == "rotate_secret" and rule.limit == 2 for rule in rotate_rules)
    assert any(rule.name == "clear_chat" for rule in command_rules)


def test_bot_middleware_drops_burst_before_handler() -> None:
    async def scenario() -> None:
        config = settings(bot_burst_rate_limit=2, bot_global_rate_limit_per_minute=5)
        middleware = BotSpamProtectionMiddleware(FakeRedis(), config)  # type: ignore[arg-type]
        handled = 0

        async def handler(_event, _data):
            nonlocal handled
            handled += 1

        for update_id in range(1, 4):
            await middleware(handler, callback_update("menu:products", update_id), {})
        assert handled == 2

    asyncio.run(scenario())
