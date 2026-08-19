"""Runs document ingestion independent of any HTTP connection.

Mirrors GenerationWorker's own rationale exactly (see that module's
docstring): the connection that hands a file to the backend must never be
the thing actually doing the work, or a client disconnect silently
discards it. This worker has its own queue, separate from
GenerationWorker's: routing ingestion recovery through GenerationWorker's
single message-at-a-time queue instead would let a slow LLM generation in
one conversation fully block another conversation's stuck-document
recovery, even though embedding and LLM inference use separate locks
(LockedEmbedder/LockedLLMDriver in src/concurrency.py) and never actually
contend for the same resource. It only ever waits for a prior embedding,
never for an unrelated LLM generation. Idle-unload of the
embedder (see run()) is deliberately gated on more than just this
worker's own idleness: as long as ANY message anywhere is still
QUEUED/RETRIEVING/GENERATING, a new document could still be dropped into
some conversation and would want to embed while that generation keeps
running — unloading in that window just guarantees paying a reload the
next time it happens, for no real RAM benefit over the span of one
still-active generation.

Document-status *reporting* to the frontend is deliberately NOT this
worker's job — src/api/routes_messages.py's own polling watcher
(_watch_documents) reads Document rows directly and reports transitions
independent of both this worker and GenerationWorker, so a conversation's
Sources panel stays live regardless of what else the app is doing.
"""

from __future__ import annotations

import asyncio
import contextlib

from src.db.repository import Repository
from src.ingestion.pipeline import IngestionPipeline
from src.logger import get_logger
from src.models import Document, DocumentStatus
from src.rag.index_manager import IndexManager

logger = get_logger(__name__)


class IngestionWorker:

    # Mirrors GenerationWorker's own idle-unload threshold: 12 empty
    # polls (500ms each) = ~6s of idle time before freeing the embedder.
    _IDLE_POLLS_BEFORE_UNLOAD = 12

    _IN_PROGRESS_STATUSES = (
        DocumentStatus.EXTRACTING,
        DocumentStatus.CHUNKING,
        DocumentStatus.EMBEDDING,
    )

    def __init__(
        self,
        repository: Repository,
        pipeline: IngestionPipeline,
        index_manager: IndexManager,
        embedder: object,
    ) -> None:
        self._repository = repository
        self._pipeline = pipeline
        self._index_manager = index_manager
        self._embedder = embedder
        self._task: asyncio.Task[None] | None = None
        self._idle_polls = 0
        self._model_unloaded = False
        # Counts consecutive times _process_document raised (an UNEXPECTED
        # failure — process_existing already contains every ordinary
        # per-file failure internally, always resolving to a terminal
        # status). Drives the backoff below so a persistently failing
        # document (e.g. a corrupted on-disk index) can't spin the loop at
        # zero delay; reset the moment any document is processed without
        # raising. Mirrors GenerationWorker's own
        # _consecutive_finalize_failures pattern exactly.
        self._consecutive_failures = 0

    def start(self) -> None:
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def recover_from_crash(self) -> None:
        """Call once at startup, before the app can serve a single request.

        UPLOADING rows are left untouched — no side effects happened yet
        (the file write already completed in create_stub, but nothing has
        read it back), and run() re-derives its queue from the DB on every
        wake, so they get picked up naturally. EXTRACTING/CHUNKING/EMBEDDING
        rows mean a worker was actively mid-pipeline when the process last
        stopped; don't guess at resuming (no chunks are written until the
        very end, but nothing here can safely tell "how far did it get"
        apart from re-running from scratch), so mark them ERROR — mirrors
        GenerationWorker.recover_from_crash's exact treatment of
        RETRIEVING/GENERATING messages.
        """
        for document in await self._repository.list_nonterminal_documents():
            if document.status in self._IN_PROGRESS_STATUSES:
                await self._repository.update_document_status(
                    document.id,
                    DocumentStatus.ERROR,
                    "Interrupted by server restart before completing.",
                )

    async def run(self) -> None:
        while True:
            try:
                document = await self._repository.get_oldest_pending_document()
                if document is not None:
                    self._idle_polls = 0
                    self._model_unloaded = False
                    await self._process_document(document)
                    self._consecutive_failures = 0
                    continue
                self._idle_polls += 1
                if (
                    self._idle_polls >= self._IDLE_POLLS_BEFORE_UNLOAD
                    and not self._model_unloaded
                    and not await self._repository.list_nonterminal_messages()
                ):
                    # Ingestion itself has nothing pending, but a message is
                    # still QUEUED/RETRIEVING/GENERATING somewhere — a new
                    # document could still be dropped into some conversation
                    # any moment and would want to embed while that
                    # generation is still running (the whole point of this
                    # worker existing). Unloading now would just mean paying
                    # the reload cost the next time that happens, for no
                    # RAM benefit worth mentioning over the span of one
                    # still-active generation. Deferring, not resetting
                    # _idle_polls — re-checked every poll, so this unloads
                    # within 500ms of the last message actually finishing.
                    await self._unload_embedder()
                    self._model_unloaded = True
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Nothing may ever escape this loop — if it did, every
                # document uploaded for the rest of the process's life would
                # sit non-terminal forever with nothing consuming the queue.
                # process_existing already contains every ORDINARY per-file
                # failure internally (always resolving to a terminal
                # status), so reaching here means something outside that
                # (e.g. index_manager.get()/save() disk I/O) broke — back
                # off exponentially rather than hammering it at zero delay.
                logger.exception("ingestion worker: unexpected error in main loop")
                self._consecutive_failures += 1
                await asyncio.sleep(min(0.05 * (2**self._consecutive_failures), 5.0))

    async def _process_document(self, document: Document) -> None:
        logger.info(
            "ingestion_job_start document_id=%s conversation=%s", document.id, document.conversation_id
        )
        index = await self._index_manager.get(document.conversation_id)
        async for _event in self._pipeline.process_existing(document, index):
            pass  # DB status is updated inline by process_existing; watchers poll the DB directly
        await self._index_manager.save(document.conversation_id)
        logger.info("ingestion_job_end document_id=%s", document.id)

    async def _unload_embedder(self) -> None:
        unload = getattr(self._embedder, "unload", None)
        if unload is None:
            return
        try:
            await unload()
        except Exception:
            logger.exception("ingestion worker: failed to unload embedder")
