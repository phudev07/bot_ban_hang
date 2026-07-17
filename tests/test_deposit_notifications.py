import asyncio
from datetime import UTC, datetime

from app.deposit_notifications import deposit_notification_text, send_deposit_notification
from app.services import PaymentResult


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


def make_result() -> PaymentResult:
    return PaymentResult(
        status="credited",
        user_id=123456789,
        amount=50_000,
        deposit_code="NAP123456789ABCD",
        username="buyer_demo",
        paid_at=datetime(2026, 7, 17, 1, 23, tzinfo=UTC),
    )


def test_deposit_notification_contains_requested_fields() -> None:
    text = deposit_notification_text(make_result())

    assert "Nạp tiền shop bot" in text
    assert "@buyer_demo đã nạp <b>50.000đ</b>" in text
    assert "NAP123456789ABCD" in text
    assert "17/07/2026 08:23:00" in text


def test_deposit_notification_is_sent_to_every_admin() -> None:
    async def scenario() -> None:
        bot = FakeBot()
        await send_deposit_notification(
            bot,  # type: ignore[arg-type]
            (11, 22),
            make_result(),
        )
        assert [chat_id for chat_id, _text in bot.messages] == [11, 22]

    asyncio.run(scenario())
