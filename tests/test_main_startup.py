import asyncio
import inspect
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.main import delete_expired_api_audits, main, wait_for_server_started
from app.models import ApiRequestAudit


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


def test_expired_api_audits_are_deleted_without_touching_recent_rows(tmp_path) -> None:
    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'audit.db').as_posix()}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            session.add_all(
                [
                    ApiRequestAudit(
                        method="GET",
                        path="/v1/products",
                        status_code=200,
                        created_at=datetime.now(UTC) - timedelta(days=31),
                    ),
                    ApiRequestAudit(
                        method="POST",
                        path="/v1/orders",
                        status_code=200,
                        created_at=datetime.now(UTC),
                    ),
                ]
            )
            await session.commit()
        assert await delete_expired_api_audits(sessions, 30) == 1
        async with sessions() as session:
            assert int(await session.scalar(select(func.count(ApiRequestAudit.id))) or 0) == 1
        await engine.dispose()

    asyncio.run(scenario())
