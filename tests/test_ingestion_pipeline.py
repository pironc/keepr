"""Tests for IngestionPipeline — specifically the content-hash dedup fix:
re-attaching a file that's already indexed in a conversation must not
duplicate its chunks in the vector index.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from src.config import Settings
from src.db.repository import Repository
from src.embeddings.mock_embedder import MockEmbedder
from src.ingestion.pipeline import DocumentStatusEvent, IngestionPipeline
from src.models import Conversation, DocumentStatus
from src.vectorstore.flat_index import NumpyFlatIndex


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    return Settings.from_env()


async def test_reattaching_the_same_file_does_not_duplicate_chunks(
    repository: Repository, embedder: MockEmbedder, settings: Settings
) -> None:
    conversation_id = "conv-dedup"
    await repository.create_conversation(Conversation(id=conversation_id, title="test"))

    pipeline = IngestionPipeline(repository, embedder, settings)
    index = NumpyFlatIndex()
    content = b"The quantized index cuts memory usage by four times."

    first_events = [
        event
        async for event in pipeline.ingest(conversation_id, "notes.txt", "text/plain", content, index)
    ]
    assert first_events[-1].status == DocumentStatus.INDEXED
    chunks_after_first = len(index)
    assert chunks_after_first > 0

    second_events = [
        event
        async for event in pipeline.ingest(conversation_id, "notes.txt", "text/plain", content, index)
    ]

    # Same document_id reported both times — it recognized the existing file
    # instead of creating a second one.
    assert second_events[-1].document_id == first_events[-1].document_id
    assert second_events[-1].status == DocumentStatus.INDEXED
    assert len(index) == chunks_after_first  # not doubled

    documents = await repository.list_documents(conversation_id)
    assert len(documents) == 1


async def test_a_genuinely_different_file_is_ingested_normally(
    repository: Repository, embedder: MockEmbedder, settings: Settings
) -> None:
    conversation_id = "conv-different-files"
    await repository.create_conversation(Conversation(id=conversation_id, title="test"))

    pipeline = IngestionPipeline(repository, embedder, settings)
    index = NumpyFlatIndex()

    await _drain(pipeline.ingest(conversation_id, "a.txt", "text/plain", b"first file content", index))
    await _drain(pipeline.ingest(conversation_id, "b.txt", "text/plain", b"second, different file", index))

    documents = await repository.list_documents(conversation_id)
    assert len(documents) == 2
    assert all(doc.status == DocumentStatus.INDEXED for doc in documents)


async def _drain(events: AsyncIterator[DocumentStatusEvent]) -> None:
    async for _ in events:
        pass
