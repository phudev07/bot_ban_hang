import asyncio
import inspect

from app.main import main, wait_for_server_started


def test_web_server_is_started_before_supplier_background_workers() -> None:
    source = inspect.getsource(main)
    ready_index = source.index("await wait_for_server_started")
    for worker_name in (
        "supplier_sync_worker(",
        "supplier_recovery_worker(",
        "supplier_audit_worker(",
        "lehai_sync_worker(",
    ):
        assert ready_index < source.index(worker_name)
    assert "await sync_sumistore_products" not in source
    assert "await sync_lehai_products" not in source
    assert "await reconcile_supplier_balance" not in source


def test_wait_for_server_started_waits_for_ready_flag() -> None:
    async def scenario() -> None:
        class FakeServer:
            started = False

        server = FakeServer()

        async def start_server() -> None:
            await asyncio.sleep(0.02)
            server.started = True

        task = asyncio.create_task(start_server())
        await wait_for_server_started(server, task, timeout_seconds=1)  # type: ignore[arg-type]
        assert server.started is True
        await task

    asyncio.run(scenario())
