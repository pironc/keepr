"""Tests for IngestionWorker: the core regression test in this file proves
the actual bug this worker exists to fix — a document's embedding must
finish independent of any OTHER conversation's LLM generation, not
blocked behind it. See src/ingestion/worker.py's own docstring for the
full rationale.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from src.config import Settings
from src.db.repository import Repository
from src.embeddings.mock_embedder import MockEmbedder
from src.ingestion.pipeline import IngestionPipeline
from src.ingestion.worker import IngestionWorker
from src.llm.base import LLMDriver
from src.models import (
    Chunk,
    Conversation,
    Document,
    DocumentStatus,
    LLMMessage,
    Message,
    MessageStatus,
    PageRef,
    SourceKind,
)
from src.rag.engine import RagEngine
from src.rag.generation_worker import GenerationWorker
from src.rag.index_manager import IndexManager
from src.vectorstore.flat_index import NumpyFlatIndex


class _BlockingDriver(LLMDriver):
    """Never completes generate() until externally signaled — holds
    GenerationWorker's single execution slot open indefinitely so a test
    can prove some OTHER unit of work finishes while this one is still in
    flight."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        self.started.set()
        await self.release.wait()
        yield "done"


class _UnloadTrackingEmbedder:
    """Wraps MockEmbedder to add an unload() the idle-unload path can call
    and count — MockEmbedder itself has none, so IngestionWorker's own
    getattr(embedder, "unload", None) check would make it a silent no-op,
    hiding exactly the behavior these tests need to observe."""

    def __init__(self) -> None:
        self._inner = MockEmbedder()
        self.dimensions = self._inner.dimensions
        self.unload_count = 0

    async def embed_documents(self, texts: list[str]) -> NDArray[np.float32]:
        return await self._inner.embed_documents(texts)

    async def embed_query(self, text: str) -> NDArray[np.float32]:
        return await self._inner.embed_query(text)

    async def availability(self) -> str | None:
        return await self._inner.availability()

    async def unload(self) -> None:
        self.unload_count += 1


def _make_ingestion_worker(
    repository: Repository, embedder: MockEmbedder, settings: Settings, tmp_path: Path
) -> IngestionWorker:
    pipeline = IngestionPipeline(repository, embedder, settings)
    index_manager = IndexManager(tmp_path / "index", "flat")
    return IngestionWorker(repository, pipeline, index_manager, embedder)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    return Settings.from_env()


async def test_document_finishes_while_an_unrelated_conversation_is_still_generating(
    repository: Repository, embedder: MockEmbedder, settings: Settings, tmp_path: Path
) -> None:
    """Regression test: uploading a file to a new chat, then leaving before
    embedding finishes, would otherwise leave it stuck forever whenever
    nothing else ever revisited that conversation. Simulates that exact
    interruption (a Document row created directly at EMBEDDING, its real
    bytes already on disk, and — critically — no message ever enqueued for
    its conversation, since a same-conversation follow-up message is the
    only thing that would otherwise retry it) alongside a SEPARATE
    conversation stuck mid-generation, and asserts the document reaches
    INDEXED while that other generation is still running, not after."""
    conversation_a = "conv-generating"
    conversation_b = "conv-stuck-doc"
    await repository.create_conversation(Conversation(id=conversation_a, title="test"))
    await repository.create_conversation(Conversation(id=conversation_b, title="test"))

    # Conversation A: a message that will block in generate() until
    # released. Needs a real indexed chunk — with nothing above the
    # (zero) similarity threshold, RagEngine refuses before ever calling
    # the driver, and the driver would never block at all.
    document_a = Document(
        id="doc-a",
        conversation_id=conversation_a,
        filename="fact.txt",
        source_kind=SourceKind.TEXT,
        content_hash="hash-fact-a",
        status=DocumentStatus.INDEXED,
    )
    await repository.create_document(document_a)
    chunk_a = Chunk(
        id="chunk-a",
        document_id=document_a.id,
        conversation_id=conversation_a,
        text="a fact for conversation a",
        source_ref=PageRef(page=1),
        chunk_index=0,
    )
    await repository.create_chunks([chunk_a])
    gen_index_dir = tmp_path / "gen-index"
    gen_index = NumpyFlatIndex()
    gen_index.add([chunk_a.id], await embedder.embed_documents([chunk_a.text]))
    await asyncio.to_thread(gen_index_dir.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(gen_index.save, gen_index_dir / f"{conversation_a}.npz")

    await repository.create_message(
        Message(id="user-a", conversation_id=conversation_a, role="user", content="a question")
    )
    await repository.create_message(
        Message(
            id="assistant-a",
            conversation_id=conversation_a,
            role="assistant",
            content="",
            status=MessageStatus.QUEUED,
        )
    )

    # Conversation B: a document interrupted mid-embed — its bytes are on
    # disk (create_stub already ran, successfully, before the connection
    # dropped) but its row never reached a terminal status. No message is
    # ever enqueued for conversation_b.
    content = b"some fact that never finished embedding"
    document_id = "stuck-doc"
    stuck = Document(
        id=document_id,
        conversation_id=conversation_b,
        filename="notes.txt",
        source_kind=SourceKind.TEXT,
        content_hash=hashlib.sha256(content).hexdigest(),
        status=DocumentStatus.EMBEDDING,
    )
    await repository.create_document(stuck)
    upload_dir = settings.upload_dir
    await asyncio.to_thread(upload_dir.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread((upload_dir / f"{document_id}_notes.txt").write_bytes, content)

    driver = _BlockingDriver()
    engine = RagEngine(repository=repository, embedder=embedder, top_k=5, min_similarity=0.0)
    generation_index_manager = IndexManager(tmp_path / "gen-index", "flat")
    generation_worker = GenerationWorker(repository, engine, generation_index_manager, driver)
    ingestion_worker = _make_ingestion_worker(repository, embedder, settings, tmp_path)

    generation_worker.start()
    ingestion_worker.start()
    try:
        await asyncio.wait_for(driver.started.wait(), timeout=2.0)

        async def _wait_indexed() -> None:
            while True:
                doc = await repository.get_document(document_id)
                assert doc is not None
                if doc.status == DocumentStatus.INDEXED:
                    return
                await asyncio.sleep(0.02)

        await asyncio.wait_for(_wait_indexed(), timeout=2.0)

        # The actual proof of concurrency: conversation A's generation is
        # still blocked, not finished, at the moment B's document indexed.
        message_a = await repository.get_message("assistant-a")
        assert message_a is not None
        assert message_a.status == MessageStatus.GENERATING
    finally:
        driver.release.set()
        await generation_worker.stop()
        await ingestion_worker.stop()


async def test_recover_from_crash_errors_in_progress_documents_but_leaves_uploading_alone(
    repository: Repository, embedder: MockEmbedder, settings: Settings, tmp_path: Path
) -> None:
    await repository.create_conversation(Conversation(id="conv-crash", title="test"))
    await repository.create_document(
        Document(
            id="mid-embed",
            conversation_id="conv-crash",
            filename="a.txt",
            source_kind=SourceKind.TEXT,
            content_hash="hash-a",
            status=DocumentStatus.EMBEDDING,
        )
    )
    await repository.create_document(
        Document(
            id="just-uploaded",
            conversation_id="conv-crash",
            filename="b.txt",
            source_kind=SourceKind.TEXT,
            content_hash="hash-b",
            status=DocumentStatus.UPLOADING,
        )
    )

    worker = _make_ingestion_worker(repository, embedder, settings, tmp_path)
    await worker.recover_from_crash()

    mid_embed_after = await repository.get_document("mid-embed")
    assert mid_embed_after is not None
    assert mid_embed_after.status == DocumentStatus.ERROR
    assert mid_embed_after.error_message is not None

    uploading_after = await repository.get_document("just-uploaded")
    assert uploading_after is not None
    assert uploading_after.status == DocumentStatus.UPLOADING  # untouched — no side effects to undo


async def test_embedder_idle_unload_defers_while_a_message_is_still_in_flight(
    repository: Repository, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: without cross-worker awareness, IngestionWorker
    would decide whether to unload the embedder purely from its OWN
    idleness, with no awareness that GenerationWorker might still be
    mid-generation for some other conversation — so a document dropped in
    shortly after would pay a needless reload, even though the embedder
    would have been useful again within moments. It must defer unloading
    while ANY message anywhere is still non-terminal, and unload promptly
    (next poll) once that clears."""
    monkeypatch.setattr(IngestionWorker, "_IDLE_POLLS_BEFORE_UNLOAD", 1)

    await repository.create_conversation(Conversation(id="conv-busy", title="test"))
    message = Message(
        id="in-flight", conversation_id="conv-busy", role="assistant", content="",
        status=MessageStatus.GENERATING,
    )
    await repository.create_message(message)

    embedder = _UnloadTrackingEmbedder()
    pipeline = IngestionPipeline(repository, embedder, settings)
    index_manager = IndexManager(tmp_path / "index", "flat")
    worker = IngestionWorker(repository, pipeline, index_manager, embedder)

    worker.start()
    try:
        # Well past the (monkeypatched, 1-poll) idle threshold — the
        # message is still GENERATING, so this must not have unloaded.
        await asyncio.sleep(0.8)
        assert embedder.unload_count == 0

        # Once the message clears, the very next poll should unload.
        await repository.finalize_message(message.id, MessageStatus.DONE, "done", [])
        await asyncio.sleep(0.8)
        assert embedder.unload_count == 1
    finally:
        await worker.stop()
