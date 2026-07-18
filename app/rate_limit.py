import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitRule:
    name: str
    limit: int
    window_seconds: int


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int = 0
    rule: str = ""


class FixedWindowRateLimiter:
    def __init__(
        self,
        redis: Redis,
        prefix: str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.redis = redis
        self.prefix = prefix
        self.clock = clock
        self._last_error_log = 0.0

    async def hit(
        self,
        scope: str,
        rules: Sequence[RateLimitRule],
        *,
        multiplier: int = 1,
    ) -> RateLimitDecision:
        now = int(self.clock())
        normalized_scope = scope.replace(" ", "_")[:160]
        try:
            for rule in rules:
                bucket = now // rule.window_seconds
                key = f"{self.prefix}:{normalized_scope}:{rule.name}:{bucket}"
                current = await self.redis.incr(key)
                if current == 1:
                    await self.redis.expire(key, rule.window_seconds + 5)
                if current > rule.limit * max(1, multiplier):
                    retry_after = rule.window_seconds - (now % rule.window_seconds)
                    return RateLimitDecision(False, max(1, retry_after), rule.name)
        except RedisError:
            current_time = self.clock()
            if current_time - self._last_error_log >= 60:
                logger.warning("Rate limiter unavailable; requests are temporarily allowed")
                self._last_error_log = current_time
        return RateLimitDecision(True)

    async def allow_notice(self, scope: str, seconds: int = 10) -> bool:
        try:
            return bool(
                await self.redis.set(
                    f"{self.prefix}:notice:{scope}"[:220],
                    "1",
                    ex=max(1, seconds),
                    nx=True,
                )
            )
        except RedisError:
            return False


def _update_subject(update: Update) -> Message | CallbackQuery | None:
    if update.callback_query is not None:
        return update.callback_query
    return update.message or update.edited_message


def _update_action(subject: Message | CallbackQuery) -> str:
    if isinstance(subject, CallbackQuery):
        data = subject.data or ""
        if data.startswith(("deposit:", "directpay:")):
            return "deposit"
        if data.startswith(("buy:", "buycoupon:")) or data == "sms:rent":
            return "purchase"
        if data == "warehouse-api:rotate-confirm":
            return "rotate_secret"
        if data == "menu:clear":
            return "clear_chat"
        return "callback"

    content = (subject.text or subject.caption or "").strip()
    if not content:
        return "message"
    command = content.split(maxsplit=1)[0].lower()
    command = command.split("@", 1)[0]
    if command == "/naptien":
        return "deposit_menu"
    if command == "/donchat":
        return "clear_chat"
    return "message"


def bot_rate_rules(subject: Message | CallbackQuery, settings: Settings) -> tuple[RateLimitRule, ...]:
    action = _update_action(subject)
    rules = [
        RateLimitRule("burst", settings.bot_burst_rate_limit, 3),
        RateLimitRule("minute", settings.bot_global_rate_limit_per_minute, 60),
    ]
    if isinstance(subject, CallbackQuery):
        rules.append(RateLimitRule("callback", 20, 10))
    else:
        rules.append(RateLimitRule("message", 15, 10))
    if action == "deposit":
        rules.append(
            RateLimitRule(
                "deposit",
                settings.bot_deposit_rate_limit_per_5_minutes,
                5 * 60,
            )
        )
    elif action == "deposit_menu":
        rules.append(RateLimitRule("deposit_menu", 8, 60))
    elif action == "purchase":
        rules.append(
            RateLimitRule("purchase", settings.bot_purchase_rate_limit_per_minute, 60)
        )
    elif action == "rotate_secret":
        rules.append(RateLimitRule("rotate_secret", 2, 10 * 60))
    elif action == "clear_chat":
        rules.append(RateLimitRule("clear_chat", 2, 60))
    return tuple(rules)


class BotSpamProtectionMiddleware(BaseMiddleware):
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.settings = settings
        self.limiter = FixedWindowRateLimiter(redis, "bot-limit")
        self.admin_ids = set(settings.admin_ids)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not self.settings.bot_spam_protection_enabled or not isinstance(event, Update):
            return await handler(event, data)
        subject = _update_subject(event)
        if subject is None or subject.from_user is None:
            return await handler(event, data)

        user_id = subject.from_user.id
        decision = await self.limiter.hit(
            f"user:{user_id}",
            bot_rate_rules(subject, self.settings),
            multiplier=4 if user_id in self.admin_ids else 1,
        )
        if decision.allowed:
            return await handler(event, data)

        if await self.limiter.allow_notice(f"user:{user_id}"):
            text = f"Bạn thao tác quá nhanh. Vui lòng chờ {decision.retry_after} giây."
            try:
                if isinstance(subject, CallbackQuery):
                    await subject.answer(text)
                else:
                    await subject.answer(text)
            except Exception:
                pass
        return None
