from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError


async def delete_recent_messages(
    bot: Bot,
    *,
    chat_id: int,
    newest_message_id: int,
    limit: int = 1000,
) -> int:
    if newest_message_id < 1 or limit < 1:
        return 0
    first_message_id = max(1, newest_message_id - limit + 1)
    message_ids = list(range(first_message_id, newest_message_id + 1))
    deleted_batches = 0
    for start in range(0, len(message_ids), 100):
        batch = message_ids[start : start + 100]
        try:
            await bot.delete_messages(chat_id=chat_id, message_ids=batch)
        except (TelegramBadRequest, TelegramForbiddenError):
            continue
        deleted_batches += 1
    return deleted_batches
