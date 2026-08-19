"""Shared fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

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


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("LLM_DRIVER", "mock")
    monkeypatch.setenv("EMBEDDER", "mock")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "keepr.db"))
    monkeypatch.setenv("INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("RETRIEVAL_MIN_SIMILARITY", "0.0")
    monkeypatch.setenv("MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("MODEL_SELECTION_PATH", str(tmp_path / "selection.json"))
    (tmp_path / "models").mkdir()

    from src.api.app import app

    with TestClient(app) as test_client:
        yield test_client
