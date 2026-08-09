"""Tests for GenerationWorker: durable processing order, one-generation-
at-a-time serialization, and resilience to a watcher disconnecting —
the mechanisms behind the "refresh mid-generation loses the answer" fix.
Each test is written to fail if the specific property it protects were
removed, matching this repo's existing convention (see test_db_pool.py,
test_rag_engine.py).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from src.concurrency import LockedEmbedder
from src.db.repository import Repository
from src.embeddings.mock_embedder import MockEmbedder
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
from src.rag.engine import DoneEvent, RagEngine
from src.rag.generation_worker import GenerationWorker
from src.rag.index_manager import IndexManager
from src.vectorstore.flat_index import NumpyFlatIndex


class _ScriptedDriver(LLMDriver):
    """Yields fixed tokens with a small delay — enough of a real window for
    a test to abandon a watch() iterator mid-stream, matching what an actual
    page refresh does to the original bug."""

    def __init__(self, tokens: list[str], delay: float = 0.01) -> None:
        self._tokens = tokens
        self._delay = delay
        self.call_count = 0

    async def generate(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        self.call_count += 1
        for token in self._tokens:
            await asyncio.sleep(self._delay)
            yield token


class _ConcurrencyTrackingDriver(LLMDriver):
    def __init__(self, tokens: list[str], delay: float = 0.02) -> None:
        self._tokens = tokens
        self._delay = delay
        self.in_flight = 0
        self.max_in_flight = 0

    async def generate(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            for token in self._tokens:
                await asyncio.sleep(self._delay)
                yield token
        finally:
            self.in_flight -= 1


class _RecordingDriver(LLMDriver):
    """Records which question it was actually called for, in call order —
    used to prove processing order without depending on wall-clock timing."""

    def __init__(self) -> None:
        self.questions_seen: list[str] = []

    async def generate(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        self.questions_seen.append(messages[-1].content)
        yield "ok"


class _RaisesOnceThenSucceedsDriver(LLMDriver):
    def __init__(self) -> None:
        self.call_count = 0

    async def generate(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        self.call_count += 1
        if self.call_count == 1:
            yield "partial"
            yield " answer"
            raise RuntimeError("simulated model crash")
        yield "a normal reply"


class _TitleAwareDriver(LLMDriver):
    """Distinguishes a RAG-answer call from a title-generation call the same
    way MockLLMDriver does — no <context> tag means it's not a RAG answer —
    so tests can assert on title generation without it being entangled with
    the main answer's own content."""

    def __init__(self) -> None:
        self.answer_call_count = 0
        self.title_call_count = 0

    async def generate(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        system_message = next((m for m in messages if m.role == "system"), None)
        if system_message is not None and "<context>" in system_message.content:
            self.answer_call_count += 1
            yield "a real answer"
        else:
            self.title_call_count += 1
            yield "Generated Title"


class _RaisingTitleDriver(LLMDriver):
    """Answers normally but always fails when asked to generate a title —
    proves title generation is best-effort and can never take the already-
    finalized main answer down with it."""

    async def generate(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        system_message = next((m for m in messages if m.role == "system"), None)
        if system_message is not None and "<context>" in system_message.content:
            yield "a real answer"
            return
        raise RuntimeError("simulated title generation failure")


class _RacyEmbedder:
    """Purpose-built to expose the exact race confirmed by reading
    llama_cpp's real source (no internal locking, multi-step mutation of
    instance state) — MockEmbedder is stateless and can never fail this way."""

    def __init__(self) -> None:
        self.dimensions = 4
        self._shared_state: str | None = None

    async def embed_documents(self, texts: list[str]) -> np.ndarray:
        return np.stack([await self._embed_one(text) for text in texts])

    async def embed_query(self, text: str) -> np.ndarray:
        return await self._embed_one(text)

    async def _embed_one(self, text: str) -> np.ndarray:
        self._shared_state = text
        await asyncio.sleep(0)  # the exact kind of yield point real inference has
        assert self._shared_state == text, (
            f"race detected: expected {text!r}, got {self._shared_state!r} — "
            "another concurrent call overwrote shared state"
        )
        return np.zeros(4, dtype=np.float32)


async def _seed_exchange(
    repository: Repository,
    conversation_id: str,
    question: str,
    created_at: datetime | None = None,
) -> str:
    """Persists a user question followed immediately by a QUEUED assistant
    placeholder — exactly the shape routes_messages.py produces, which is
    what GenerationWorker._run_one reconstructs a job from."""
    if await repository.get_conversation(conversation_id) is None:
        await repository.create_conversation(Conversation(id=conversation_id, title="test"))
    base = created_at or datetime.now(UTC)
    await repository.create_message(
        Message(
            id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            role="user",
            content=question,
            created_at=base,
        )
    )
    placeholder = Message(
        id=str(uuid.uuid4()),
        conversation_id=conversation_id,
        role="assistant",
        content="",
        status=MessageStatus.QUEUED,
        created_at=base + timedelta(microseconds=1),
    )
    await repository.create_message(placeholder)
    return placeholder.id


async def _seed_chunk_with_index(
    repository: Repository, embedder: MockEmbedder, conversation_id: str, index_dir: Path, text: str
) -> None:
    # Idempotent regardless of call order relative to _seed_exchange (which
    # also creates the conversation) — PRAGMA foreign_keys=ON means a
    # Document row referencing a not-yet-existing conversation_id fails.
    if await repository.get_conversation(conversation_id) is None:
        await repository.create_conversation(Conversation(id=conversation_id, title="test"))
    document = Document(
        id=f"{conversation_id}-doc",
        conversation_id=conversation_id,
        filename="manual.pdf",
        source_kind=SourceKind.PDF,
        content_hash="test-hash",
        status=DocumentStatus.INDEXED,
    )
    await repository.create_document(document)
    chunk = Chunk(
        id=f"{conversation_id}-chunk",
        document_id=document.id,
        conversation_id=conversation_id,
        text=text,
        source_ref=PageRef(page=1),
        chunk_index=0,
    )
    await repository.create_chunks([chunk])
    index = NumpyFlatIndex()
    index.add([chunk.id], await embedder.embed_documents([chunk.text]))
    await asyncio.to_thread(index_dir.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(index.save, index_dir / f"{conversation_id}.npz")


def _make_worker(
    repository: Repository, embedder: MockEmbedder, driver: LLMDriver, tmp_path: Path
) -> GenerationWorker:
    from unittest.mock import MagicMock

    engine = RagEngine(repository=repository, embedder=embedder, top_k=5, min_similarity=0.0)
    index_manager = IndexManager(tmp_path / "index", "flat")
    # The fallback ingestion path (GenerationWorker._ensure_documents_indexed)
    # is not exercised by any test in this file — every test explicitly
    # pre-populates the index before enqueuing.  Pass a mock pipeline so
    # the constructor is satisfied without real Settings / embedder deps.
    mock_pipeline = MagicMock()
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir(exist_ok=True)
    return GenerationWorker(repository, engine, index_manager, driver, mock_pipeline, upload_dir, embedder)


async def _wait_until_terminal(repository: Repository, message_id: str, timeout_seconds: float = 5.0) -> Message:
    async def _poll() -> Message:
        while True:
            message = await repository.get_message(message_id)
            assert message is not None
            if message.status in (MessageStatus.DONE, MessageStatus.ERROR):
                return message
            await asyncio.sleep(0.01)

    try:
        async with asyncio.timeout(timeout_seconds):
            return await _poll()
    except TimeoutError:
        raise AssertionError(f"message {message_id} never reached a terminal status") from None


async def test_abandoning_the_watch_does_not_stop_generation(
    repository: Repository, embedder: MockEmbedder, tmp_path: Path
) -> None:
    driver = _ScriptedDriver(["The", " answer", " is", " 42."])
    worker = _make_worker(repository, embedder, driver, tmp_path)
    await _seed_chunk_with_index(repository, embedder, "conv-1", tmp_path / "index", "42 is the answer.")
    message_id = await _seed_exchange(repository, "conv-1", "what is the answer")
    worker.start()
    try:
        # No explicit cleanup on the abandoned iterator — a real page
        # refresh doesn't politely close anything either, it just yanks the
        # connection away. The property under test is that _run_one's task
        # keeps going regardless, not that watch() was closed nicely.
        seen = 0
        async for _event in worker.watch(message_id):
            seen += 1
            if seen >= 2:
                break

        message = await _wait_until_terminal(repository, message_id)
        assert message.status == MessageStatus.DONE
        assert message.content.strip() != ""
    finally:
        await worker.stop()


async def test_two_watchers_see_identical_content_from_one_generation(
    repository: Repository, embedder: MockEmbedder, tmp_path: Path
) -> None:
    driver = _ScriptedDriver(["Paris", " is", " the", " capital."])
    worker = _make_worker(repository, embedder, driver, tmp_path)
    await _seed_chunk_with_index(repository, embedder, "conv-2", tmp_path / "index", "Paris is the capital.")
    message_id = await _seed_exchange(repository, "conv-2", "capital of france")
    worker.start()
    try:

        async def collect(iterator: AsyncIterator[object]) -> list[object]:
            return [event async for event in iterator]

        results = await asyncio.gather(collect(worker.watch(message_id)), collect(worker.watch(message_id)))
        done_a = next(e for e in results[0] if isinstance(e, DoneEvent))
        done_b = next(e for e in results[1] if isinstance(e, DoneEvent))
        assert done_a.message.content == done_b.message.content
        assert done_a.message.content.strip() != ""
        assert driver.call_count == 1
    finally:
        await worker.stop()


async def test_generation_never_runs_concurrently(
    repository: Repository, embedder: MockEmbedder, tmp_path: Path
) -> None:
    driver = _ConcurrencyTrackingDriver(["hello", " world"])
    worker = _make_worker(repository, embedder, driver, tmp_path)
    await _seed_chunk_with_index(repository, embedder, "conv-3a", tmp_path / "index", "fact one.")
    await _seed_chunk_with_index(repository, embedder, "conv-3b", tmp_path / "index", "fact two.")
    id_a = await _seed_exchange(repository, "conv-3a", "question one")
    id_b = await _seed_exchange(repository, "conv-3b", "question two")
    worker.start()
    try:
        await _wait_until_terminal(repository, id_a)
        await _wait_until_terminal(repository, id_b)
        assert driver.max_in_flight == 1
    finally:
        await worker.stop()


async def test_processing_order_follows_creation_time_not_seed_order(
    repository: Repository, embedder: MockEmbedder, tmp_path: Path
) -> None:
    driver = _RecordingDriver()
    worker = _make_worker(repository, embedder, driver, tmp_path)
    base = datetime(2020, 1, 1, tzinfo=UTC)
    await _seed_chunk_with_index(repository, embedder, "conv-later", tmp_path / "index", "later fact.")
    await _seed_chunk_with_index(repository, embedder, "conv-earlier", tmp_path / "index", "earlier fact.")
    # Seed the one with the LATER created_at FIRST — the opposite of the
    # order it should actually be processed in.
    later_id = await _seed_exchange(
        repository, "conv-later", "later question", created_at=base + timedelta(seconds=10)
    )
    earlier_id = await _seed_exchange(repository, "conv-earlier", "earlier question", created_at=base)
    worker.start()
    try:
        await _wait_until_terminal(repository, later_id)
        await _wait_until_terminal(repository, earlier_id)
        assert driver.questions_seen == ["earlier question", "later question"]
    finally:
        await worker.stop()


async def test_recover_from_crash_errors_stuck_generation_but_leaves_queued_alone(
    repository: Repository, embedder: MockEmbedder, tmp_path: Path
) -> None:
    driver = _ScriptedDriver(["ok"])
    worker = _make_worker(repository, embedder, driver, tmp_path)
    await repository.create_conversation(Conversation(id="conv-crash", title="test"))
    await repository.create_message(
        Message(
            id="stuck",
            conversation_id="conv-crash",
            role="assistant",
            content="partial",
            status=MessageStatus.GENERATING,
        )
    )
    await repository.create_message(
        Message(
            id="queued",
            conversation_id="conv-crash",
            role="assistant",
            content="",
            status=MessageStatus.QUEUED,
        )
    )

    await worker.recover_from_crash()

    stuck_after = await repository.get_message("stuck")
    assert stuck_after is not None
    assert stuck_after.status == MessageStatus.ERROR
    assert stuck_after.error_message is not None
    assert stuck_after.content == "partial"  # preserved, not wiped

    queued_after = await repository.get_message("queued")
    assert queued_after is not None
    assert queued_after.status == MessageStatus.QUEUED  # untouched — no side effects to undo


async def test_mid_stream_exception_preserves_partial_content_and_worker_survives(
    repository: Repository, embedder: MockEmbedder, tmp_path: Path
) -> None:
    driver = _RaisesOnceThenSucceedsDriver()
    worker = _make_worker(repository, embedder, driver, tmp_path)
    await _seed_chunk_with_index(repository, embedder, "conv-err", tmp_path / "index", "some fact.")
    first_id = await _seed_exchange(repository, "conv-err", "question one")
    worker.start()
    try:
        failed = await _wait_until_terminal(repository, first_id)
        assert failed.status == MessageStatus.ERROR
        assert "partial answer" in failed.content
        assert failed.error_message is not None

        # Proves the worker's own loop survived the exception rather than
        # dying silently — a second job on the SAME worker still completes.
        # The worker polls for new jobs every 500ms when idle, so this gets
        # picked up automatically within that window.
        second_id = await _seed_exchange(repository, "conv-err", "question two")
        second = await _wait_until_terminal(repository, second_id, timeout_seconds=2.0)
        assert second.status == MessageStatus.DONE
        assert "a normal reply" in second.content
    finally:
        await worker.stop()


async def test_locked_embedder_prevents_the_pre_existing_concurrency_race() -> None:
    """Regression test for the race confirmed by reading llama_cpp's actual
    source: no internal locking, multi-step mutation of instance state.
    Without LockedEmbedder's lock, these two concurrent calls interleave at
    the injected yield point and one clobbers the other's shared state,
    failing the assertion inside _RacyEmbedder._embed_one."""
    inner = _RacyEmbedder()
    locked = LockedEmbedder(inner, asyncio.Lock())

    await asyncio.gather(
        locked.embed_query("first query"),
        locked.embed_query("second query"),
    )


async def test_cancelled_embedder_does_not_release_lock_until_thread_finishes() -> None:
    """Regression test for the double-cancellation crash (GGML_ASSERT in
    ggml-cpu/repack.cpp).

    Starlette's streaming-response cleanup can fire ``task.cancel()`` more
    than once (disconnect handler, then the ``finally`` block around
    ``task_group``).  If the second cancel escapes ``_run_sync_uncancellable``
    it propagates through ``LockedEmbedder.__aexit__``, releases the lock
    while the thread is still touching the model, and the very next caller
    walks straight into a two-thread-same-model concurrency crash.

    This test proves the lock stays held through multiple cancellations and
    the eventual embedding completes normally.
    """
    from unittest.mock import MagicMock

    from src.embeddings.llama_cpp_embedder import _run_sync_uncancellable

    lock = asyncio.Lock()
    thread_running: dict[str, bool] = {}

    def slow_work(seconds: float) -> None:
        thread_running["started"] = True
        import time
        time.sleep(seconds)
        thread_running["finished"] = True

    async def protected_work() -> None:
        async with lock:
            await _run_sync_uncancellable(slow_work, 0.5)

    task = asyncio.create_task(protected_work())
    await asyncio.sleep(0.05)  # let the task acquire the lock + dispatch the thread

    assert thread_running.get("started") is True
    assert lock.locked() is True

    # First cancel — Starlette's disconnect handler
    task.cancel()
    await asyncio.sleep(0.02)
    assert lock.locked() is True, "lock released after FIRST cancel — bug"

    # Second cancel — Starlette's finally: task.cancel()
    task.cancel()
    await asyncio.sleep(0.02)
    assert lock.locked() is True, "lock released after SECOND cancel — bug"

    # Thread should still be running, lock should still be held
    assert thread_running.get("finished") is not True
    assert lock.locked() is True

    # Wait for the thread to finish
    await asyncio.wait_for(task, timeout=2.0)
    assert thread_running.get("finished") is True
    assert lock.locked() is False, "lock should be released after normal completion"
    assert task.done() and not task.cancelled(), "task should complete normally, not stay cancelled"
