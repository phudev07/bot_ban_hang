import asyncio

from app.chat_cleanup import delete_recent_messages


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[list[int]] = []

    async def delete_messages(self, *, chat_id: int, message_ids: list[int]) -> None:
        assert chat_id == 123
        self.calls.append(message_ids)


def test_chat_cleanup_deletes_recent_messages_in_telegram_batches() -> None:
    async def scenario() -> None:
        bot = FakeBot()
        batches = await delete_recent_messages(
            bot,  # type: ignore[arg-type]
            chat_id=123,
            newest_message_id=1200,
            limit=1000,
        )

        assert batches == 10
        assert bot.calls[0] == list(range(201, 301))
        assert bot.calls[-1] == list(range(1101, 1201))
        assert all(len(batch) == 100 for batch in bot.calls)

    asyncio.run(scenario())
