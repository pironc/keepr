"""Tests for IngestionPipeline — specifically the content-hash dedup fix:
re-attaching a file that's already indexed in a conversation must not
duplicate its chunks in the vector index.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from src.config import Settings
from src.db.repository import Repository
from src.embeddings.mock_embedder import MockEmbedder
from src.ingestion.pipeline import DocumentStatusEvent, IngestionPipeline
from src.model_unavailable import ModelUnavailableError
from src.models import Conversation, Document, DocumentStatus, SourceKind
from src.vectorstore.flat_index import NumpyFlatIndex


class _RaisingEmbedder:
    """Minimal :class:`Embedder` twin whose embed_documents fails in one of
    the two ways we test: a semantic ModelUnavailableError or a generic
    Exception. The real protocol is duck-typed so a bare class with the right
    methods is all the pipeline needs."""

    dimensions = 4

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def embed_documents(self, texts):  # type: ignore[no-untyped-def]
        raise self._exc

    async def embed_query(self, text):  # type: ignore[no-untyped-def]
        raise self._exc

    async def availability(self):  # type: ignore[no-untyped-def]
        return None


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

    document = await pipeline.create_stub(conversation_id, "notes.txt", content)
    first_events = [event async for event in pipeline.process_existing(document, index)]
    assert first_events[-1].status == DocumentStatus.INDEXED
    chunks_after_first = len(index)
    assert chunks_after_first > 0

    # A real caller (IngestionWorker) only ever calls process_existing on a
    # document create_stub actually returned as pending — this asserts that
    # for an already-INDEXED file, create_stub alone is enough to prevent
    # re-ingestion, without needing process_existing to defend against it.
    reattached = await pipeline.create_stub(conversation_id, "notes.txt", content)
    assert reattached.id == document.id
    assert reattached.status == DocumentStatus.INDEXED
    assert len(index) == chunks_after_first  # nothing re-embedded

    documents = await repository.list_documents(conversation_id)
    assert len(documents) == 1


async def test_create_stub_reuses_an_existing_non_terminal_row(
    repository: Repository, settings: Settings
) -> None:
    """A document interrupted mid-pipeline (e.g. an earlier attempt left it
    at EMBEDDING) must not get a second, duplicate row if the same content
    is handed to create_stub again — otherwise IngestionWorker would end up
    processing two rows for what's really one file, doubling its chunks."""
    conversation_id = "conv-resume"
    await repository.create_conversation(Conversation(id=conversation_id, title="test"))
    content = b"some content that was interrupted mid-embed"
    content_hash = hashlib.sha256(content).hexdigest()
    stuck = Document(
        id="stuck-doc",
        conversation_id=conversation_id,
        filename="notes.txt",
        source_kind=SourceKind.TEXT,
        content_hash=content_hash,
        status=DocumentStatus.EMBEDDING,
    )
    await repository.create_document(stuck)

    pipeline = IngestionPipeline(repository, MockEmbedder(), settings)
    reused = await pipeline.create_stub(conversation_id, "notes.txt", content)

    assert reused.id == "stuck-doc"
    documents = await repository.list_documents(conversation_id)
    assert len(documents) == 1


async def test_a_genuinely_different_file_is_ingested_normally(
    repository: Repository, embedder: MockEmbedder, settings: Settings
) -> None:
    conversation_id = "conv-different-files"
    await repository.create_conversation(Conversation(id=conversation_id, title="test"))

    pipeline = IngestionPipeline(repository, embedder, settings)
    index = NumpyFlatIndex()

    doc_a = await pipeline.create_stub(conversation_id, "a.txt", b"first file content")
    await _drain(pipeline.process_existing(doc_a, index))
    doc_b = await pipeline.create_stub(conversation_id, "b.txt", b"second, different file")
    await _drain(pipeline.process_existing(doc_b, index))

    documents = await repository.list_documents(conversation_id)
    assert len(documents) == 2
    assert all(doc.status == DocumentStatus.INDEXED for doc in documents)


async def _drain(events: AsyncIterator[DocumentStatusEvent]) -> None:
    async for _ in events:
        pass


async def test_missing_model_embedding_failure_marks_doc_error_without_crashing(
    repository: Repository, settings: Settings
) -> None:
    """A ModelUnavailableError from the embedder (missing/downloadable model)
    must be contained: the document ends up ERROR with an actionable message
    and the pipeline does NOT re-raise — an uncontained failure here would
    escape into the SSE stream and crash GenerationWorker."""
    conversation_id = "conv-missing-model"
    await repository.create_conversation(Conversation(id=conversation_id, title="test"))

    embedder = _RaisingEmbedder(ModelUnavailableError("Embedding model file not found: models/nomic.gguf"))
    pipeline = IngestionPipeline(repository, embedder, settings)
    index = NumpyFlatIndex()
    content = b"some content"

    document = await pipeline.create_stub(conversation_id, "notes.txt", content)
    events = [event async for event in pipeline.process_existing(document, index)]

    # The pipeline yields an ERROR progress event and returns normally.
    assert events[-1].status == DocumentStatus.ERROR
    assert events[-1].error_message is not None
    assert "Embedding model file not found" in events[-1].error_message
    assert len(index) == 0  # nothing indexed

    # The persisted document row is also ERROR, and the readable message is
    # stored in error_message (what the UI surfaces on the failed source).
    documents = await repository.list_documents(conversation_id)
    assert len(documents) == 1
    assert documents[0].status == DocumentStatus.ERROR
    assert documents[0].error_message is not None
    assert "Embedding model file not found" in documents[0].error_message


async def test_corrupt_model_embedding_failure_is_also_contained_to_doc_error(
    repository: Repository, settings: Settings
) -> None:
    """A corrupt/truncated model surfaces as a wrapped ModelUnavailableError;
    the pipeline still contains it (marking the doc ERROR) rather than letting
    a raw embedder exception escape the streaming generator."""
    conversation_id = "conv-corrupt-model"
    await repository.create_conversation(Conversation(id=conversation_id, title="test"))

    embedder = _RaisingEmbedder(
        ModelUnavailableError("Embedding model could not be loaded — the file may be corrupted")
    )
    pipeline = IngestionPipeline(repository, embedder, settings)
    content = b"some content"

    document = await pipeline.create_stub(conversation_id, "notes.txt", content)
    events = [event async for event in pipeline.process_existing(document, NumpyFlatIndex())]
    assert events[-1].status == DocumentStatus.ERROR
    assert events[-1].error_message is not None
    assert "corrupted" in events[-1].error_message


async def test_generic_embedder_exception_is_contained_without_crashing(
    repository: Repository, settings: Settings
) -> None:
    """Any non-semantic embedder exception is also contained (marked ERROR) so
    a novel/mystery embedding bug can never regress into a raw SSE crash."""
    conversation_id = "conv-generic-error"
    await repository.create_conversation(Conversation(id=conversation_id, title="test"))

    pipeline = IngestionPipeline(repository, _RaisingEmbedder(RuntimeError("boom")), settings)
    content = b"some content"
    document = await pipeline.create_stub(conversation_id, "notes.txt", content)
    events = [event async for event in pipeline.process_existing(document, NumpyFlatIndex())]
    assert events[-1].status == DocumentStatus.ERROR
    assert events[-1].error_message is not None
    assert "boom" in events[-1].error_message
