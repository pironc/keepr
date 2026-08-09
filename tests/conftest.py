"""Shared fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from src.db.pool import SQLiteConnectionPool
from src.db.repository import Repository
from src.embeddings.mock_embedder import MockEmbedder
from src.llm.mock_driver import MockLLMDriver


@pytest_asyncio.fixture
async def db_pool(tmp_path: Path) -> AsyncIterator[SQLiteConnectionPool]:
    pool = SQLiteConnectionPool(tmp_path / "test.db")
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def repository(db_pool: SQLiteConnectionPool) -> Repository:
    repo = Repository(db_pool)
    await repo.initialize()
    return repo


@pytest.fixture
def embedder() -> MockEmbedder:
    return MockEmbedder(dimensions=32)


@pytest.fixture
def llm_driver() -> MockLLMDriver:
    return MockLLMDriver()
