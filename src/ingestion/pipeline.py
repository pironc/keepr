"""Orchestrates Ingestor -> chunker -> embedder -> index for one uploaded
file, emitting status transitions as it goes — this is exactly what
drives the UI's per-file processing animation over SSE, and it's the same
sequence regardless of source type, which is the point.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from src.config import Settings
from src.db.repository import Repository
from src.embeddings.base import Embedder
from src.ingestion.base import UnsupportedSourceError
from src.ingestion.chunker import chunk_segments
from src.ingestion.registry import find_ingestor
from src.logger import get_logger
from src.models import Chunk, Document, DocumentStatus, SourceKind
from src.vectorstore.base import VectorIndex

logger = get_logger(__name__)

_VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm")
_AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a")


@dataclass(slots=True, frozen=True)
class DocumentStatusEvent:
    document_id: str
    status: DocumentStatus
    error_message: str | None = None


def _source_kind_for(filename: str) -> SourceKind:
    lower = filename.lower()
    if lower.endswith(".pdf"):
        return SourceKind.PDF
    if lower.endswith(_VIDEO_EXTENSIONS):
        return SourceKind.VIDEO
    if lower.endswith(_AUDIO_EXTENSIONS):
        return SourceKind.AUDIO
    return SourceKind.TEXT


class IngestionPipeline:
    def __init__(self, repository: Repository, embedder: Embedder, settings: Settings) -> None:
        self._repository = repository
        self._embedder = embedder
        self._settings = settings
        # Serialise ingestion per conversation — the SSE generator in
        # routes_messages.py and the GenerationWorker's
        # _ensure_documents_indexed both call ingest() for the same
        # document concurrently, which races through every DB operation
        # and creates duplicate chunks in the index.  A per-conversation
        # lock ensures the worker's call waits for the SSE generator to
        # finish, finds the document already INDEXED, and returns
        # immediately instead of re-running the full pipeline.
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _get_lock(self, conversation_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            if conversation_id not in self._locks:
                self._locks[conversation_id] = asyncio.Lock()
            return self._locks[conversation_id]

    async def ingest(
        self,
        conversation_id: str,
        filename: str,
        mime_type: str,
        content: bytes,
        index: VectorIndex,
    ) -> AsyncIterator[DocumentStatusEvent]:
        # Only one ingest() call per conversation at a time.  The
        # GenerationWorker's _ensure_documents_indexed fallback races
        # with the SSE generator's primary ingestion path — this lock
        # makes the worker wait until the SSE generator finishes, at
        # which point the content-hash check below finds the document
        # already INDEXED and returns immediately without re-running
        # the full pipeline.
        async with await self._get_lock(conversation_id):
            async for event in self._ingest(
                conversation_id, filename, mime_type, content, index
            ):
                yield event

    async def _ingest(
        self,
        conversation_id: str,
        filename: str,
        mime_type: str,
        content: bytes,
        index: VectorIndex,
    ) -> AsyncIterator[DocumentStatusEvent]:
        content_hash = hashlib.sha256(content).hexdigest()
        existing = await self._repository.get_document_by_content_hash(conversation_id, content_hash)
        if existing is not None and existing.status == DocumentStatus.INDEXED:
            # This exact file is already indexed in this conversation (e.g.
            # re-dropped on a later message) — re-ingesting would duplicate
            # every one of its chunks in the index. Report it as already
            # done instead of silently doing the work twice.
            yield DocumentStatusEvent(existing.id, DocumentStatus.UPLOADING)
            yield DocumentStatusEvent(existing.id, DocumentStatus.INDEXED)
            return

        # If a document stub was already created (e.g. by the SSE generator
        # or a previous interrupted ingestion attempt), reuse it instead of
        # inserting a duplicate row — otherwise the same file ends up with
        # two sets of chunks in the index.
        if existing is not None:
            document = existing
        else:
            document = Document(
                id=str(uuid.uuid4()),
                conversation_id=conversation_id,
                filename=filename,
                source_kind=_source_kind_for(filename),
                content_hash=content_hash,
                status=DocumentStatus.UPLOADING,
            )
            await self._repository.create_document(document)
        yield DocumentStatusEvent(document.id, DocumentStatus.UPLOADING)

        ingestor = find_ingestor(filename, mime_type)
        if ingestor is None:
            async for event in self._fail(document.id, DocumentStatus.UNSUPPORTED, f"Unsupported file type: {filename}"):
                yield event
            return

        path = self._settings.upload_dir / f"{document.id}_{filename}"
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, content)

        await self._repository.update_document_status(document.id, DocumentStatus.EXTRACTING)
        yield DocumentStatusEvent(document.id, DocumentStatus.EXTRACTING)
        try:
            segments = await ingestor.extract(path)
        except UnsupportedSourceError as exc:
            async for event in self._fail(document.id, DocumentStatus.UNSUPPORTED, str(exc)):
                yield event
            return
        except Exception as exc:
            logger.warning("extraction failed for document %s: %s", document.id, exc)
            async for event in self._fail(document.id, DocumentStatus.ERROR, str(exc)):
                yield event
            return

        await self._repository.update_document_status(document.id, DocumentStatus.CHUNKING)
        yield DocumentStatusEvent(document.id, DocumentStatus.CHUNKING)
        chunked = chunk_segments(segments, self._settings.chunk_size, self._settings.chunk_overlap)
        if not chunked:
            async for event in self._fail(
                document.id, DocumentStatus.ERROR, "No extractable text found in this file"
            ):
                yield event
            return

        chunks = [
            Chunk(
                id=str(uuid.uuid4()),
                document_id=document.id,
                conversation_id=conversation_id,
                text=segment.text,
                source_ref=segment.source_ref,
                chunk_index=chunk_index,
            )
            for chunk_index, segment in enumerate(chunked)
        ]

        await self._repository.update_document_status(document.id, DocumentStatus.EMBEDDING)
        yield DocumentStatusEvent(document.id, DocumentStatus.EMBEDDING)
        vectors = await self._embedder.embed_documents([chunk.text for chunk in chunks])

        await self._repository.create_chunks(chunks)
        index.add([chunk.id for chunk in chunks], vectors)

        await self._repository.update_document_status(document.id, DocumentStatus.INDEXED)
        yield DocumentStatusEvent(document.id, DocumentStatus.INDEXED)

    async def _fail(
        self, document_id: str, status: DocumentStatus, error_message: str
    ) -> AsyncIterator[DocumentStatusEvent]:
        await self._repository.update_document_status(document_id, status, error_message)
        yield DocumentStatusEvent(document_id, status, error_message)
