"""Runs message generation independent of any HTTP connection.

If a StreamingResponse's async generator were the thing that both talks
to the LLM and persists the final answer, a client disconnect (e.g. a
page refresh) would tear down that generator before the answer is ever
saved, not just before it's shown. Making generation a background task
with a lifetime independent of any request means a disconnect can only
ever kill a *subscriber* relaying events to one dead socket, never the
generation itself.

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

Document ingestion is NOT this worker's job — IngestionWorker
(src/ingestion/worker.py) owns the extract->chunk->embed->index queue
entirely, on its own schedule, independent of whatever this worker is
doing. Before retrieval, `_wait_for_documents_ready` only *waits* for this
conversation's documents to reach a terminal status (a cheap DB poll) — it
does no embedding work itself and reports none of ingestion's own,
per-document progress to any watcher (still entirely routes_messages.py's
`_watch_documents`/Sources-panel job, independent of this worker, so a
conversation's Sources panel stays live regardless of what this worker is
doing for some other conversation — see CLAUDE.md's "Rule #1" for why that
separation matters). It does report one coarser fact at the *message*
level: MessageStatus.PROCESSING_DOCUMENTS, set once if this job's wait
actually has anything to wait for, so "why is my message not moving" reads
as "waiting on your documents" rather than a misleading generic "Queued"
when nothing else is actually queued at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from src.db.repository import Repository
from src.llm.base import LLMDriver
from src.logger import get_logger
from src.model_unavailable import ModelUnavailableError
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


WorkerEvent = RagEvent | ConversationTitleEvent


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
    ) -> None:
        self._repository = repository
        self._engine = engine
        self._index_manager = index_manager
        self._driver = driver
        self._condition = asyncio.Condition()
        self._current: _ActiveSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._idle_polls = 0
        self._models_unloaded = False
        # See _process_job: counts consecutive times a job's own attempt to
        # record its ERROR status has *also* failed (observed live as a
        # second "database is locked" right after the first). Drives the
        # backoff in run() below — reset to 0 the moment any job completes
        # its bookkeeping normally, success or handled failure alike.
        self._consecutive_finalize_failures = 0

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
        picked up naturally. PROCESSING_DOCUMENTS rows get reverted to
        QUEUED for the exact same reason: that status only ever means "this
        job's own _wait_for_documents_ready hasn't returned yet" — no model
        state has been touched — but get_oldest_queued_message() filters on
        literal QUEUED, so leaving a row at PROCESSING_DOCUMENTS instead
        would strand it forever (never picked up again, never marked
        ERROR either). RETRIEVING/GENERATING rows mean a worker was
        actively touching the model when the process last stopped; don't
        guess at resuming, mark them ERROR so they read as a clear,
        resendable failure instead of spinning forever.
        """
        for message in await self._repository.list_nonterminal_messages():
            if message.status == MessageStatus.PROCESSING_DOCUMENTS:
                await self._repository.update_message_status(message.id, MessageStatus.QUEUED)
            elif message.status in (MessageStatus.RETRIEVING, MessageStatus.GENERATING):
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
                    # If it's NOT clearing (_consecutive_finalize_failures
                    # keeps climbing — the same job can't even record its
                    # own failure, so it stays QUEUED and comes right back),
                    # back off exponentially instead of hammering at a flat
                    # 50ms forever — capped at 5s, matching the pool's own
                    # busy_timeout, so a lock that's merely slow to clear
                    # still gets picked up promptly once it does.
                    delay = 0.05 * (2**self._consecutive_finalize_failures)
                    await asyncio.sleep(min(delay, 5.0))
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
            try:
                # finalize_message itself now retries through "database is
                # locked" (see its own comment in repository.py) — this is
                # no longer a bare, unprotected attempt. What's left to guard
                # against here is that method exhausting ALL of its own
                # retries: unlike a job that fails during
                # _wait_for_documents_ready (still QUEUED at that point, so
                # get_oldest_queued_message() picks it right back up), a job
                # that fails after update_message_status has already moved it
                # to RETRIEVING/GENERATING has no such fallback — that query
                # only ever finds QUEUED rows. If finalize_message is the
                # ONLY thing that can ever move it to a terminal status and
                # every one of its retries still fails, the message is stuck
                # forever with nothing else that will ever retry it — so
                # catch that here rather than letting it crash the loop.
                await self._repository.finalize_message(
                    job.id,
                    MessageStatus.ERROR,
                    "",
                    [],
                    error_message=f"Unexpected worker error: {exc}",
                )
                await self._repository.touch_conversation(job.conversation_id)
            except Exception:
                logger.error(
                    "generation worker: could not record job %s as ERROR even after "
                    "finalize_message's own retries — this message will remain stuck "
                    "at a non-terminal status",
                    job.id,
                )
                self._consecutive_finalize_failures += 1
                return
            async with self._condition:
                if self._current is not None and self._current.message_id == job.id:
                    self._current.events.append(
                        MessageStatusEvent(job.id, MessageStatus.ERROR, str(exc))
                    )
                    self._current.finished = True
                    self._current = None
                self._condition.notify_all()
        self._consecutive_finalize_failures = 0
        logger.info("worker_job_end message_id=%s", job.id)

    async def _wait_for_documents_ready(self, job: Message, session: _ActiveSession) -> None:
        """Block until every document in this conversation has reached a
        terminal ingestion status, without doing any of that work itself.

        IngestionWorker (src/ingestion/worker.py) owns the actual
        extract->chunk->embed->index pipeline entirely, on its own queue,
        independent of this worker. This is purely a correctness gate —
        retrieval can't run against a document that isn't indexed yet.  It
        still reports nothing about *how far along* ingestion is — that
        stays routes_messages.py's `_watch_documents`/Sources-panel job
        alone, live regardless of whether this worker is busy with some
        other conversation's LLM generation. The one thing this DOES report
        (once, the first time it actually has to wait — never on every 0.2s
        poll tick) is the message-level fact that it's waiting at all: see
        MessageStatus.PROCESSING_DOCUMENTS's own docstring for why that
        distinction from QUEUED matters. Reverted back to QUEUED on crash
        recovery below if the process dies mid-wait — no side effects have
        happened yet, so it's exactly as safe to resume from scratch as a
        row that never left QUEUED in the first place.
        """
        terminal = frozenset({DocumentStatus.INDEXED, DocumentStatus.ERROR, DocumentStatus.UNSUPPORTED})
        announced = False
        while True:
            docs = await self._repository.list_documents(job.conversation_id)
            if all(doc.status in terminal for doc in docs):
                return
            if not announced:
                announced = True
                await self._repository.update_message_status(job.id, MessageStatus.PROCESSING_DOCUMENTS)
                async with self._condition:
                    session.events.append(
                        MessageStatusEvent(job.id, MessageStatus.PROCESSING_DOCUMENTS)
                    )
                    self._condition.notify_all()
            await asyncio.sleep(0.2)

    async def _unload_models(self) -> None:
        """Unload the LLM model to free RAM.

        Called once after the worker has been idle for
        _IDLE_POLLS_BEFORE_UNLOAD consecutive polls (~6 s). The model is
        lazily re-loaded on the next request that needs it, so the only
        cost is a one-time load delay on the first token of the next job.
        The embedder has its own idle-unload timing, owned by
        IngestionWorker — this worker's own idleness says nothing about
        whether ingestion is idle too, and vice versa.

        unload() is best-effort: if the underlying model doesn't support
        unloading (MockLLMDriver) the LockedLLMDriver wrapper makes it a
        silent no-op.
        """
        logger.info("generation worker: idle — unloading model to free RAM")
        unload = getattr(self._driver, "unload", None)
        if unload is None:
            return
        try:
            await unload()
        except Exception:
            logger.exception("generation worker: failed to unload %s", type(self._driver).__name__)

    async def _run_one(self, job: Message) -> None:
        # The session is tied to this message_id and buffers every
        # RagEvent/ConversationTitleEvent from here on so a watcher that
        # joins mid-generation sees everything so far. Created before
        # _wait_for_documents_ready purely for that buffering — document
        # status itself no longer flows through this session at all (see
        # routes_messages.py's _watch_documents).
        session = _ActiveSession(message_id=job.id)
        async with self._condition:
            self._current = session
            self._condition.notify_all()

        # Correctness gate, not ingestion work: IngestionWorker processes
        # documents on its own schedule; this just waits until this
        # conversation's are all terminal before retrieving from them.
        await self._wait_for_documents_ready(job, session)

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
        except ModelUnavailableError:
            # The language model isn't installed — already surfaced in the
            # answer message; don't spam the log with a traceback.
            logger.info(
                "generation worker: skipping title generation for conversation %s — "
                "language model unavailable",
                conversation_id,
            )
            return
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
                    # session events are already gone. Document status isn't
                    # this worker's concern at all (see
                    # routes_messages.py's _watch_documents, which handles
                    # late-joining watchers for that independently), so there
                    # is nothing to reconstruct here beyond the DoneEvent.
                    yield DoneEvent(message=message)
                    return
                if not announced_queued:
                    yield MessageStatusEvent(message_id, message.status)
                    announced_queued = True
            async with self._condition:
                await self._condition.wait()
