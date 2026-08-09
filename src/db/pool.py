"""Async SQLite connection pool with guaranteed teardown.

Same design as used in a companion project (sentinel): connections are
lazily opened, a partial failure during startup closes whatever was
already opened instead of stranding it, and `close()` drains the pool
before closing so an in-flight `acquire()` caller is never closed out
from under it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import TracebackType

import aiosqlite


class SQLiteConnectionPool:
    def __init__(self, db_path: Path, pool_size: int = 5) -> None:
        self._db_path = db_path
        self._pool_size = pool_size
        self._pool: asyncio.Queue[aiosqlite.Connection] | None = None
        self._connections: list[aiosqlite.Connection] = []
        self._start_lock = asyncio.Lock()

    async def _ensure_started(self) -> asyncio.Queue[aiosqlite.Connection]:
        if self._pool is not None:
            return self._pool
        async with self._start_lock:
            if self._pool is not None:
                return self._pool
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            pool: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue(maxsize=self._pool_size)
            opened: list[aiosqlite.Connection] = []
            try:
                for _ in range(self._pool_size):
                    connection = await aiosqlite.connect(self._db_path)
                    connection.row_factory = aiosqlite.Row
                    await connection.execute("PRAGMA foreign_keys = ON")
                    # WAL mode: writers never block readers, readers never
                    # block writers — this is the single biggest lever for
                    # SQLite concurrency.  Pair with synchronous=NORMAL
                    # (the standard recommendation with WAL) to avoid the
                    # extra fsync that FULL does on top of WAL's own fsyncs.
                    await connection.execute("PRAGMA journal_mode = WAL")
                    await connection.execute("PRAGMA synchronous = NORMAL")
                    # Without a busy_timeout, any concurrent write to the same
                    # database (different connection, same process) fails
                    # immediately with "database is locked" instead of waiting
                    # for the other writer to finish.  5 s is enough for even
                    # the slowest embedding pass to complete a single INSERT.
                    await connection.execute("PRAGMA busy_timeout = 5000")
                    opened.append(connection)
                    pool.put_nowait(connection)
            except Exception:
                for connection in opened:
                    await connection.close()
                raise
            self._connections = opened
            self._pool = pool
            return pool

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[aiosqlite.Connection]:
        pool = await self._ensure_started()
        connection = await pool.get()
        try:
            yield connection
        finally:
            pool.put_nowait(connection)

    async def close(self) -> None:
        if self._pool is None:
            return
        for _ in range(len(self._connections)):
            connection = await self._pool.get()
            await connection.close()
        self._connections.clear()
        self._pool = None

    async def __aenter__(self) -> SQLiteConnectionPool:
        await self._ensure_started()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
