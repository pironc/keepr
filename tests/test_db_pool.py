"""Direct tests for SQLiteConnectionPool's failure-handling and shutdown
semantics — the two subtlest properties don't show up in ordinary
happy-path usage."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from src.db.pool import SQLiteConnectionPool


async def test_pool_start_failure_closes_partial_connections_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = SQLiteConnectionPool(tmp_path / "test.db", pool_size=3)

    real_connect = aiosqlite.connect
    opened: list[aiosqlite.Connection] = []
    calls = 0

    async def flaky_connect(*args: Any, **kwargs: Any) -> aiosqlite.Connection:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated transient failure")
        connection = await real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr("src.db.pool.aiosqlite.connect", flaky_connect)

    with pytest.raises(RuntimeError):
        async with pool.acquire():
            pass

    assert len(opened) == 1
    with pytest.raises(Exception):  # noqa: B017 - aiosqlite raises on a closed connection
        await opened[0].execute("SELECT 1")
    assert pool._pool is None
    assert pool._connections == []

    monkeypatch.setattr("src.db.pool.aiosqlite.connect", real_connect)
    async with pool.acquire() as connection:
        await connection.execute("SELECT 1")
    await pool.close()


async def test_close_waits_for_an_in_flight_connection_to_be_released(tmp_path: Path) -> None:
    pool = SQLiteConnectionPool(tmp_path / "test.db", pool_size=1)
    holding = asyncio.Event()
    released = asyncio.Event()

    async def holder() -> None:
        async with pool.acquire():
            holding.set()
            await released.wait()

    holder_task = asyncio.create_task(holder())
    await holding.wait()

    close_task = asyncio.create_task(pool.close())
    await asyncio.sleep(0.01)
    assert not close_task.done(), "close() must block while a connection is still checked out"

    released.set()
    await holder_task
    await close_task
