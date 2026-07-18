import asyncio

import httpx

from app.rentsim import RentSimClient


def services_payload(stock: int = 12) -> dict[str, object]:
    return {
        "us2": [
            {"name": "Microsoft", "value": "microsoft", "price": 600, "stock": 100}
        ],
        "kh2": [
            {"name": "ChatGPT", "value": "chatgpt", "price": 1_000, "stock": stock}
        ],
    }


def test_rentsim_snapshot_uses_only_kh2_chatgpt_and_limits_stock_by_balance() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/phone/services":
            return httpx.Response(200, json=services_payload(stock=12))
        assert request.url.path == "/secret-test"
        return httpx.Response(200, json={"id": "123", "balance": "3500"})

    async def scenario() -> None:
        client = RentSimClient(
            "http://supplier.test",
            "secret-test",
            transport=httpx.MockTransport(handler),
        )
        snapshot = await client.fetch_snapshot(force=True)

        assert snapshot.server_id == "kh2"
        assert snapshot.service_id == "chatgpt"
        assert snapshot.unit_price == 1_000
        assert snapshot.source_stock == 12
        assert snapshot.balance == 3_500
        assert snapshot.effective_stock == 3

    asyncio.run(scenario())


def test_rentsim_rent_and_otp_flow_use_cambodia_server() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/chatgpt/secret-test":
            assert request.url.params["server"] == "kh2"
            return httpx.Response(
                200,
                json={
                    "status": "Pending",
                    "id": "ORDER-1",
                    "phoneNumber": "+85512345678",
                    "phonenoprefix": "012 345 678",
                    "coutrycode": "+855",
                    "serviceName": "chatgpt",
                },
            )
        assert request.url.path == "/api/order/ORDER-1/secret-test"
        return httpx.Response(
            200,
            json={
                "status": "Successed",
                "id": "ORDER-1",
                "content": "123456 is your ChatGPT verification code.",
                "code": "123456",
                "serviceName": "chatgpt",
            },
        )

    async def scenario() -> None:
        client = RentSimClient(
            "http://supplier.test",
            "secret-test",
            transport=httpx.MockTransport(handler),
        )
        rental = await client.rent()
        otp = await client.fetch_otp(rental.order_id)

        assert rental.status == "pending"
        assert rental.phone_number == "+85512345678"
        assert otp.status == "success"
        assert otp.code == "123456"

    asyncio.run(scenario())


def test_rentsim_timeout_is_a_terminal_otp_result() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "error", "message": "Timeout"})

    async def scenario() -> None:
        client = RentSimClient(
            "http://supplier.test",
            "secret-test",
            transport=httpx.MockTransport(handler),
        )
        otp = await client.fetch_otp("ORDER-TIMEOUT")
        assert otp.status == "timeout"

    asyncio.run(scenario())
