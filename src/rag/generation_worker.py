"""Runs message generation independent of any HTTP connection.

The bug this exists to fix: today, a StreamingResponse's async generator
*is* the thing that both talks to the LLM and persists the final answer —
so a client disconnect (page refresh) tears down the generator (confirmed
via Starlette's own StreamingResponse.stream_response) before the answer
is ever saved, not just before it's shown. Making generation a background
task with a lifetime independent of any request means a disconnect can
only ever kill a *subscriber* relaying events to one dead socket, never
the generation itself.

Processing order is always re-derived from the DB (`get_oldest_queued_message`,
ordered by `created_at, rowid`), never trusted from asyncio scheduling or
lock-acquisition order — two concurrent requests creating placeholders and
racing to run does not guarantee whichever acquires first matches which
one was actually asked first.

Only one job is ever active app-wide (there is one shared LLM instance
behind `RagEngine`), so live-watching uses a single nullable `_current`
slot plus a condition variable rather than a per-subscriber queue
registry — any number of watchers just track their own read-offset over
the same shared, growing event log. This also means a slow or dead
watcher can never back-pressure the worker: the worker only ever mutates
shared state and calls `notify_all()`, it never pushes to a subscriber
directly.

File ingestion runs in the SSE generator (routes_messages.py) as the
primary path — documents go through the full pipeline immediately, even
while another chat is mid-generation.  This worker's
_ensure_documents_indexed serves as a fallback: if the client disconnected
during ingestion (page refresh, navigation), any documents that never
reached INDEXED are finished here before retrieval.  The two paths are
safe to interleave because LockedEmbedder and LockedLLMDriver use separate
asyncio.Lock instances, and SQLite busy_timeout absorbs concurrent writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from src.db.repository import Repository
from src.ingestion.pipeline import DocumentStatusEvent, IngestionPipeline
from src.llm.base import LLMDriver
from src.logger import get_logger
from src.models import DocumentStatus, LLMMessage, Message, MessageStatus
from src.rag.engine import DoneEvent, MessageStatusEvent, RagEngine, RagEvent, TokenEvent
from src.rag.index_manager import IndexManager
from src.rag.title import generate_title

logger = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class ConversationTitleEvent:
    """Emitted at most once per conversation, after its first exchange
    finishes — see GenerationWorker._maybe_title_conversation."""

    conversation_id: str
    title: str


WorkerEvent = RagEvent | ConversationTitleEvent | DocumentStatusEvent


@dataclass(slots=True)
class _ActiveSession:
    message_id: str
    events: list[WorkerEvent] = field(default_factory=list)
    finished: bool = False


class GenerationWorker:

    # Number of consecutive empty polls (500ms each) before unloading
    # the LLM and embedding models to free RAM.  12 polls = ~6 seconds
    # of idle time.  The models are lazily re-loaded on next use, so
    # the only cost of unloading is a one-time load delay on the first
    # token of the next request — well worth it for freeing gigabytes
    # of RAM between conversations.
    _IDLE_POLLS_BEFORE_UNLOAD = 12

    def __init__(
        self,
        repository: Repository,
        engine: RagEngine,
        index_manager: IndexManager,
        driver: LLMDriver,
        pipeline: IngestionPipeline,
        upload_dir: Path,
        embedder: object,
    ) -> None:
        self._repository = repository
        self._engine = engine
        self._index_manager = index_manager
        self._driver = driver
        self._pipeline = pipeline
        self._upload_dir = upload_dir
        self._embedder = embedder
        self._condition = asyncio.Condition()
        self._current: _ActiveSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._idle_polls = 0
        self._models_unloaded = False

    def start(self) -> None:
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def recover_from_crash(self) -> None:
        """Call once at startup, before the app can serve a single request.

        QUEUED rows are left untouched — no side effects happened yet, and
        `run()` re-derives its queue from the DB on every wake, so they get
        picked up naturally. RETRIEVING/GENERATING rows mean a worker was
        actively touching the model when the process last stopped; don't
        guess at resuming, mark them ERROR so they read as a clear,
        resendable failure instead of spinning forever.
        """
        for message in await self._repository.list_nonterminal_messages():
            if message.status in (MessageStatus.RETRIEVING, MessageStatus.GENERATING):
                await self._repository.finalize_message(
                    message.id,
                    MessageStatus.ERROR,
                    message.content,
                    message.citations,
                    error_message="Interrupted by server restart before completing.",
                )

    async def enqueue_new(self, conversation_id: str) -> str:
        """Call AFTER the triggering user Message is already persisted.

        Deliberately takes no question/history/index/driver — those are
        all re-derived from durable state when the job actually runs (see
        `_run_one`), which is what makes a QUEUED row resumable after a
        server restart with zero information lost.
        """
        message = Message(
            id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            role="assistant",
            content="",
            status=MessageStatus.QUEUED,
        )
        await self._repository.create_message(message)
        return message.id

    async def run(self) -> None:
        while True:
            try:
                job = await self._repository.get_oldest_queued_message()
                if job is not None:
                    self._idle_polls = 0
                    self._models_unloaded = False
                    await self._process_job(job)
                    # A short breath before polling for the next job.
                    # Without this, a failing job that can't even write its
                    # ERROR status (e.g. "database is locked") stays QUEUED
                    # and the loop retries it immediately — zero backoff,
                    # infinite storm, hammering the already-locked DB.
                    # 50ms is imperceptible for sequential normal jobs and
                    # enough to let a transient lock clear between retries.
                    await asyncio.sleep(0.05)
                    continue
                # Idle: poll every 500ms.  An asyncio.Event + double-check
                # pattern (clear → recheck → wait) has a narrow but real race
                # window: if enqueue() runs between clear() and the second DB
                # check (the DB query yields control), doorbell.set() gets
                # erased and the worker sleeps through a queued message
                # forever, or at least until the next unrelated enqueue.
                # A short sleep is simple, correct, and the 500ms worst-case
                # latency for picking up a job is imperceptible to a user.
                self._idle_polls += 1
                if self._idle_polls >= self._IDLE_POLLS_BEFORE_UNLOAD and not self._models_unloaded:
                    await self._unload_models()
                    self._models_unloaded = True
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Nothing may ever escape this loop — if it did, every
                # message submitted for the rest of the process's life would
                # sit QUEUED forever with nothing consuming the queue.
                logger.exception("generation worker: unexpected error in main loop")

    async def _process_job(self, job: Message) -> None:
        logger.info("worker_job_start message_id=%s conversation=%s", job.id, job.conversation_id)
        try:
            await self._run_one(job)
        except Exception as exc:
            logger.exception("generation worker: job %s failed unexpectedly", job.id)
            await self._repository.finalize_message(
                job.id,
                MessageStatus.ERROR,
                "",
                [],
                error_message=f"Unexpected worker error: {exc}",
            )
            await self._repository.touch_conversation(job.conversation_id)
            async with self._condition:
                if self._current is not None and self._current.message_id == job.id:
                    self._current.events.append(
                        MessageStatusEvent(job.id, MessageStatus.ERROR, str(exc))
                    )
                    self._current.finished = True
                    self._current = None
                self._condition.notify_all()
        logger.info("worker_job_end message_id=%s", job.id)

    async def _ensure_documents_indexed(self, conversation_id: str) -> None:
        """Finish any documents that never reached INDEXED (fallback).

        The SSE generator in routes_messages.py runs the ingestion pipeline
        as the primary path, and it enqueues the assistant placeholder BEFORE
        starting ingestion — so even if the client disconnects mid-ingestion
        (page refresh, navigation), the QUEUED row already exists and the
        worker will pick up this conversation.  This method then reads the
        saved file bytes, re-runs extraction/chunking/embedding/indexing, and
        emits DocumentStatusEvent progress events through the watch() channel.
        Idempotent because `ingest()` checks the content hash: already-INDEXED
        documents are skipped, and for documents in a non-terminal state the
        existing document row is reused while the pipeline is re-run from the
        saved file bytes.

        There is a narrow race window when ingestion of a large file takes
        longer than the worker's 500ms poll interval: the SSE generator may
        still be ingesting when the worker enters this method, leading to two
        parallel ingest() calls for the same document.  The pipeline's
        content-hash dedup prevents a duplicate document row, but both calls
        can create duplicate chunks in the index.  The consequence is
        duplicate citations rather than data loss; a future staleness check
        (using document updated_at) would close this window entirely.
        """
        index = await self._index_manager.get(conversation_id)
        docs = await self._repository.list_documents(conversation_id)
        terminal = frozenset({DocumentStatus.INDEXED, DocumentStatus.ERROR, DocumentStatus.UNSUPPORTED})
        for doc in docs:
            if doc.status in terminal:
                continue
            path = self._upload_dir / f"{doc.id}_{doc.filename}"
            try:
                content = await asyncio.to_thread(path.read_bytes)
            except (FileNotFoundError, OSError):
                logger.warning(
                    "generation worker: cannot read %s for document %s — marking ERROR",
                    path, doc.id,
                )
                error_msg = (
                    "File not found on disk — may have been deleted or the upload was interrupted."
                )
                await self._repository.update_document_status(doc.id, DocumentStatus.ERROR, error_msg)
                self._emit_event(DocumentStatusEvent(doc.id, DocumentStatus.ERROR, error_msg))
                continue
            async for status_event in self._pipeline.ingest(
                conversation_id, doc.filename, "application/octet-stream", content, index
            ):
                self._emit_event(status_event)
        await self._index_manager.save(conversation_id)

    def _emit_event(self, event: WorkerEvent) -> None:
        """Publish an event into the active session so any watcher (live SSE
        or reconnect) sees it."""
        if self._current is None:
            return
        self._current.events.append(event)

    async def _unload_models(self) -> None:
        """Unload both the LLM and embedding models to free RAM.

        Called once after the worker has been idle for
        _IDLE_POLLS_BEFORE_UNLOAD consecutive polls (~6 s).  Both models
        are lazily re-loaded on the next request that needs them, so the
        only cost is a one-time load delay on the first token (or first
        embedding) of the next job.

        Each component's unload() is best-effort: if the underlying model
        doesn't support unloading (MockLLMDriver, MockEmbedder) the
        Locked* wrapper makes it a silent no-op.
        """
        logger.info("generation worker: idle — unloading models to free RAM")
        for component in (self._driver, self._embedder):
            unload = getattr(component, "unload", None)
            if unload is None:
                continue
            try:
                await unload()
            except Exception:
                logger.exception("generation worker: failed to unload %s", type(component).__name__)

    async def _run_one(self, job: Message) -> None:
        # Create the session BEFORE _ensure_documents_indexed — otherwise
        # _emit_event() drops every DocumentStatusEvent from the ingestion
        # pipeline because self._current is still None.  The session is
        # tied to this message_id; watchers that join during ingestion will
        # already see buffered document-status progress.
        session = _ActiveSession(message_id=job.id)
        async with self._condition:
            self._current = session
            self._condition.notify_all()

        # Finish any documents that never reached INDEXED — idempotent:
        # already-INDEXED documents are skipped via content-hash dedup
        # inside ingest().  Document status events emitted during ingestion
        # are buffered in session.events (self._current is now set).
        await self._ensure_documents_indexed(job.conversation_id)

        index = await self._index_manager.get(job.conversation_id)
        all_messages = await self._repository.list_messages(job.conversation_id)
        placeholder_index = next(i for i, m in enumerate(all_messages) if m.id == job.id)
        # The question is always the message immediately before this
        # placeholder — routes_messages.py persists the user's turn, then
        # enqueues the assistant placeholder, in that order. Re-deriving
        # both question and history from durable state (rather than
        # threading them through in-memory at enqueue time) is what makes a
        # QUEUED row resumable after a restart with nothing lost.
        question = all_messages[placeholder_index - 1].content
        logger.info(
            "worker_rag_start message_id=%s conversation=%s question=%.120s",
            job.id, job.conversation_id, question,
        )
        history = [
            LLMMessage(role=m.role, content=m.content)
            for m in all_messages[: placeholder_index - 1]
        ]

        async for event in self._engine.answer(
            job.conversation_id, question, history, index, self._driver, message_id=job.id
        ):
            async with self._condition:
                session.events.append(event)
                self._condition.notify_all()
                # finished is deliberately NOT flipped here (even for a
                # DoneEvent) — see the comment below, right before it's
                # actually set, for why that has to wait.

        await self._repository.touch_conversation(job.conversation_id)
        logger.info(
            "worker_rag_done message_id=%s tokens=%d",
            job.id,
            sum(1 for e in session.events if isinstance(e, TokenEvent)),
        )
        # Runs before `finished` is ever set to True — a watcher returns as
        # soon as it sees finished, which closes this specific message's SSE
        # connection (including the original sender's, mid-request). Title
        # generation needs a moment on that same connection to reach the
        # client, so it has to happen while the stream is still open, not
        # after — an extra second or two of "Generating…" on a brand new
        # conversation's first reply, never on any later one.
        await self._maybe_title_conversation(job.conversation_id, question, session)

        async with self._condition:
            # Must flip together in the SAME lock acquisition — otherwise a
            # watcher can wake, see the DoneEvent with finished still False,
            # loop back around, and see it a second time once finished
            # catches up via the DB-fallback path in watch()'s else branch.
            session.finished = True
            self._current = None
            self._condition.notify_all()

    async def _maybe_title_conversation(
        self, conversation_id: str, question: str, session: _ActiveSession
    ) -> None:
        """Best-effort and non-fatal: this runs after the message above was
        already finalized successfully, so a failure here must never affect
        it.  Only runs when the conversation has documents — text-only chats
        keep the first-message title the frontend already set.  Gated on the
        title still matching the current question (the frontend sets it to
        the first message before we ever get here), which also ensures this
        only fires once: after we replace it with an LLM-generated title the
        next exchange won't match.
        """
        conversation = await self._repository.get_conversation(conversation_id)
        if conversation is None:
            return

        # Text-only chats don't need an LLM-generated title — the first
        # user message (set by the frontend) is already good enough.
        docs = await self._repository.list_documents(conversation_id)
        if not docs:
            return

        # Only replace the title if it still matches the question this
        # exchange is answering — i.e. the frontend's first-message
        # default hasn't been overwritten yet (by a previous LLM call or
        # a manual rename).
        if conversation.title != question:
            return

        try:
            title = await generate_title(question, self._driver)
        except Exception:
            logger.exception(
                "generation worker: title generation failed for conversation %s", conversation_id
            )
            return
        await self._repository.update_conversation_title(conversation_id, title)
        async with self._condition:
            session.events.append(ConversationTitleEvent(conversation_id, title))
            self._condition.notify_all()

    async def watch(self, message_id: str) -> AsyncIterator[WorkerEvent]:
        offset = 0
        announced_queued = False
        # A DoneEvent no longer means "nothing more is coming" by itself —
        # _run_one keeps the session open a bit longer afterward for
        # best-effort title generation. Tracked locally (not on the shared
        # session — a second watcher may join after this one has already
        # seen it) so that once _current is later cleared, this watcher
        # returns instead of falling into the else branch and re-deriving a
        # second, duplicate DoneEvent from the DB.
        seen_done = False
        while True:
            async with self._condition:
                current = self._current
            if current is not None and current.message_id == message_id:
                async with self._condition:
                    new_events = current.events[offset:]
                    offset = len(current.events)
                    finished = current.finished
                for event in new_events:
                    yield event
                    if isinstance(event, DoneEvent):
                        seen_done = True
                if finished:
                    return
            elif seen_done:
                return
            else:
                message = await self._repository.get_message(message_id)
                if message is None:
                    return
                if message.status in (MessageStatus.DONE, MessageStatus.ERROR):
                    # The worker finished before this watcher joined — its
                    # session events (including document status events from
                    # _ensure_documents_indexed) are already gone.  Reconstruct
                    # the document state from the DB so the SSE stream still
                    # shows per-file progress for late-joining watchers.
                    docs = await self._repository.list_documents(message.conversation_id)
                    for doc in docs:
                        yield DocumentStatusEvent(doc.id, doc.status, doc.error_message)
                    yield DoneEvent(message=message)
                    return
                if not announced_queued:
                    yield MessageStatusEvent(message_id, message.status)
                    announced_queued = True
            async with self._condition:
                await self._condition.wait()
