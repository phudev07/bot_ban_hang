import asyncio

import httpx
import pytest

from app.autosms import AutoSmsClient
from app.rentsim import RentSimError


def test_autosms_balance_rent_and_otp_flow_use_us_chatgpt() -> None:
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        assert request.url.params["key"] == "secret-test"
        if request.url.path == "/api/balance":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"balance": 5_000, "status": "active"},
                },
            )
        if request.url.path == "/api/buy-number/us/chatgpt":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "phone": "+17276283075",
                        "price": 1_000,
                        "order_id": "AUTO-ORDER-1",
                    },
                },
            )
        assert request.url.path == "/api/orders/AUTO-ORDER-1"
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "id": "AUTO-ORDER-1",
                    "phone": "+17276283075",
                    "service": "ChatGPT",
                    "price": "1000",
                    "status": "success",
                    "code": "6404",
                    "message": "6404 is your ChatGPT verification code.",
                    "remaining_seconds": 0,
                },
            },
        )

    async def scenario() -> None:
        client = AutoSmsClient(
            "https://supplier.test",
            "secret-test",
            transport=httpx.MockTransport(handler),
        )
        snapshot = await client.fetch_snapshot(force=True)
        rental = await client.rent()
        otp = await client.fetch_otp(rental.order_id)

        assert snapshot.server_id == "us"
        assert snapshot.unit_price == 1_000
        assert snapshot.effective_stock == 5
        assert rental.phone_number == "+17276283075"
        assert rental.unit_price == 1_000
        assert otp.status == "success"
        assert otp.code == "6404"
        assert requests == [
            "/api/balance",
            "/api/buy-number/us/chatgpt",
            "/api/orders/AUTO-ORDER-1",
        ]
        await client.aclose()

    asyncio.run(scenario())


def test_autosms_timeout_cancels_order_before_refund_result() -> None:
    cancelled: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/orders/AUTO-ORDER-2":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "id": "AUTO-ORDER-2",
                        "status": "pending",
                        "code": None,
                        "remaining_seconds": 0,
                    },
                },
            )
        assert request.url.path == "/api/cancel/AUTO-ORDER-2"
        cancelled.append(request.url.path)
        return httpx.Response(
            200,
            json={"success": True, "message": "Đã hủy đơn hàng và hoàn tiền."},
        )

    async def scenario() -> None:
        client = AutoSmsClient(
            "https://supplier.test",
            "secret-test",
            transport=httpx.MockTransport(handler),
        )
        otp = await client.fetch_otp("AUTO-ORDER-2")
        assert otp.status == "timeout"
        assert cancelled == ["/api/cancel/AUTO-ORDER-2"]
        await client.aclose()

    asyncio.run(scenario())


def test_autosms_invalid_key_is_normalized() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "success": False,
                "message": "API Key không hợp lệ hoặc tài khoản không tồn tại.",
            },
        )

    async def scenario() -> None:
        client = AutoSmsClient(
            "https://supplier.test",
            "bad-key",
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(RentSimError) as caught:
            await client.fetch_balance()
        assert caught.value.code == "INVALID_KEY"
        await client.aclose()

    asyncio.run(scenario())


def test_autosms_out_of_stock_temporarily_hides_rent_button_capacity() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/buy-number/us/chatgpt":
            return httpx.Response(
                409,
                json={"success": False, "message": "Dịch vụ hiện đã hết số."},
            )
        assert request.url.path == "/api/balance"
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"balance": 20_000, "status": "active"},
            },
        )

    async def scenario() -> None:
        client = AutoSmsClient(
            "https://supplier.test",
            "secret-test",
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(RentSimError) as caught:
            await client.rent()
        assert caught.value.code == "OUT_OF_STOCK"

        snapshot = await client.fetch_snapshot(force=True)
        assert snapshot.balance == 20_000
        assert snapshot.effective_stock == 0
        await client.aclose()

    asyncio.run(scenario())
