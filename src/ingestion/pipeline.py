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
from src.model_unavailable import ModelUnavailableError
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
        # Guards create_stub's body only (a SELECT + conditional INSERT +
        # one file write — sub-millisecond) against two near-simultaneous
        # uploads of identical content (e.g. two browser tabs) racing the
        # content-hash dedup check into duplicate rows. The heavier
        # extract->chunk->embed->index pipeline (process_existing) has
        # exactly one caller app-wide (IngestionWorker, one document at a
        # time) and needs no lock of its own.
        self._create_stub_lock = asyncio.Lock()

    async def create_stub(
        self,
        conversation_id: str,
        filename: str,
        content: bytes,
    ) -> Document:
        """Fast, side-effect-light: dedup by content hash, then create-or-
        reuse the Document row and write bytes to disk. Does NOT run
        extraction/chunking/embedding — that's process_existing's job,
        called exclusively by IngestionWorker. Safe to call from a plain
        (non-streaming) request handler even under a client disconnect.
        """
        async with self._create_stub_lock:
            content_hash = hashlib.sha256(content).hexdigest()
            existing = await self._repository.get_document_by_content_hash(conversation_id, content_hash)
            if existing is not None:
                # Already indexed (e.g. re-dropped on a later message) or
                # still mid-pipeline from an earlier interrupted attempt —
                # either way, reuse this row rather than inserting a
                # duplicate, which would double every one of its chunks in
                # the index once processed.
                return existing

            document = Document(
                id=str(uuid.uuid4()),
                conversation_id=conversation_id,
                filename=filename,
                source_kind=_source_kind_for(filename),
                content_hash=content_hash,
                status=DocumentStatus.UPLOADING,
            )
            await self._repository.create_document(document)
            path = self._settings.upload_dir / f"{document.id}_{filename}"
            await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_bytes, content)
            return document

    async def process_existing(
        self,
        document: Document,
        index: VectorIndex,
    ) -> AsyncIterator[DocumentStatusEvent]:
        """Runs extract -> chunk -> embed -> index for a Document row that
        already exists (created via create_stub, bytes already on disk).
        Called exclusively by IngestionWorker — one document at a time,
        app-wide."""
        yield DocumentStatusEvent(document.id, DocumentStatus.UPLOADING)

        path = self._settings.upload_dir / f"{document.id}_{document.filename}"
        if not await asyncio.to_thread(path.exists):
            # The conversation's document row exists but its bytes don't —
            # e.g. deleted from disk, or create_stub's own write was
            # interrupted before this document ever got here. A plain
            # FileNotFoundError from ingestor.extract() below would still be
            # caught by the generic except-Exception further down, but with
            # a much less actionable message than this one.
            async for event in self._fail(
                document.id, DocumentStatus.ERROR,
                "File not found on disk — may have been deleted or the upload was interrupted.",
            ):
                yield event
            return

        # mime_type is never load-bearing here: every Ingestor.supports()
        # checks the filename extension first, falling back to mime_type
        # only for extension-less names, and _source_kind_for (used at
        # create_stub time, above) is filename-only too. A fixed value
        # matches what re-ingestion from disk has always used.
        ingestor = find_ingestor(document.filename, "application/octet-stream")
        if ingestor is None:
            async for event in self._fail(
                document.id, DocumentStatus.UNSUPPORTED, f"Unsupported file type: {document.filename}"
            ):
                yield event
            return

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
                conversation_id=document.conversation_id,
                text=segment.text,
                source_ref=segment.source_ref,
                chunk_index=chunk_index,
            )
            for chunk_index, segment in enumerate(chunked)
        ]

        await self._repository.update_document_status(document.id, DocumentStatus.EMBEDDING)
        yield DocumentStatusEvent(document.id, DocumentStatus.EMBEDDING)
        try:
            vectors = await self._embedder.embed_documents([chunk.text for chunk in chunks])
        except ModelUnavailableError as exc:
            # Embedding model missing or unloadable — this is a "your model is
            # broken" condition, not a per-file problem. Catch it here (rather
            # than letting it escape) so neither the SSE generator nor the
            # GenerationWorker crashes on it; a raw llama-cpp ValueError would
            # otherwise surface as a generic "Something went wrong" with no
            # hint about the model. Chained `error_message` is what the UI
            # shows on the failed source.
            async for event in self._fail(document.id, DocumentStatus.ERROR, str(exc)):
                yield event
            return
        except Exception as exc:  # any other embedding failure, keep contained
            logger.warning("embedding failed for document %s: %s", document.id, exc)
            async for event in self._fail(document.id, DocumentStatus.ERROR, str(exc)):
                yield event
            return

        await self._repository.create_chunks(chunks)
        index.add([chunk.id for chunk in chunks], vectors)

        await self._repository.update_document_status(document.id, DocumentStatus.INDEXED)
        yield DocumentStatusEvent(document.id, DocumentStatus.INDEXED)

    async def _fail(
        self, document_id: str, status: DocumentStatus, error_message: str
    ) -> AsyncIterator[DocumentStatusEvent]:
        await self._repository.update_document_status(document_id, status, error_message)
        yield DocumentStatusEvent(document_id, status, error_message)
