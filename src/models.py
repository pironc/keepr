"""Shared Pydantic v2 models: the single source of truth for every wire and storage shape.

`SourceRef` is a discriminated union (`PageRef` today, `TimeRef` for audio/video
later) — this is the one detail that makes citations "growable": adding a real
audio/video ingestor later only ever produces a new `TimeRef` variant, it never
requires touching `Chunk`, `Citation`, or anything downstream of them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class DocumentStatus(StrEnum):
    UPLOADING = "uploading"
    EXTRACTING = "extracting"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXED = "indexed"
    ERROR = "error"
    UNSUPPORTED = "unsupported"


class MessageStatus(StrEnum):
    """QUEUED, PROCESSING_DOCUMENTS, and RETRIEVING are deliberately
    distinct: QUEUED means a GenerationWorker job exists but hasn't been
    picked up yet — genuinely waiting its turn behind another generation,
    since only one runs at a time app-wide. PROCESSING_DOCUMENTS means the
    worker HAS picked this job up (nothing is ahead of it anymore) but is
    blocked on IngestionWorker finishing this conversation's attached
    document(s) — a real, separate wait, not the generation queue at all
    (see GenerationWorker._wait_for_documents_ready). RETRIEVING means it
    has the generation lock and is actively embedding/searching. Without
    PROCESSING_DOCUMENTS, a message sent alongside a freshly-uploaded
    document would show "Queued" for the entire extract/chunk/embed/index
    duration, reading as "waiting behind other work" when nothing else is
    actually queued at all. Defaults to DONE on Message (see below) so
    every user message — which has no lifecycle at all — needs zero
    special-casing."""

    QUEUED = "queued"
    PROCESSING_DOCUMENTS = "processing-documents"
    RETRIEVING = "retrieving"
    GENERATING = "generating"
    DONE = "done"
    ERROR = "error"


class SourceKind(StrEnum):
    PDF = "pdf"
    TEXT = "text"
    AUDIO = "audio"
    VIDEO = "video"


class PageRef(BaseModel):
    kind: Literal["page"] = "page"
    page: int


class TimeRef(BaseModel):
    kind: Literal["time"] = "time"
    start_seconds: float
    end_seconds: float


SourceRef = PageRef | TimeRef


class TextSegment(BaseModel):
    """One unit of extracted text plus where it came from in the source file."""

    text: str
    source_ref: SourceRef = Field(discriminator="kind")


class Chunk(BaseModel):
    id: str
    document_id: str
    conversation_id: str
    text: str
    source_ref: SourceRef = Field(discriminator="kind")
    chunk_index: int


class Document(BaseModel):
    id: str
    conversation_id: str
    filename: str
    source_kind: SourceKind
    content_hash: str
    status: DocumentStatus
    error_message: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Citation(BaseModel):
    chunk_id: str
    document_id: str
    document_filename: str
    source_ref: SourceRef = Field(discriminator="kind")
    snippet: str


Role = Literal["system", "user", "assistant"]


class LLMMessage(BaseModel):
    """The minimal role+content shape an LLMDriver needs — no id, no
    citations, no persistence concerns. `Message` (below) is the richer,
    persisted chat message; this is purely a driver-input wire shape."""

    role: Role
    content: str


class Message(BaseModel):
    id: str
    conversation_id: str
    role: Role
    content: str
    citations: list[Citation] = Field(default_factory=list)
    status: MessageStatus = MessageStatus.DONE
    error_message: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


DEFAULT_CONVERSATION_TITLE = "New conversation"


class Conversation(BaseModel):
    id: str
    title: str
    pinned: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
