"""POST /conversations/{id}/messages — accepts a prompt plus optional file
attachments, and streams back a single multiplexed SSE response:
document_status events (drives the UI's per-file processing animation)
for any file in the conversation, then message_status/token/citations/done
events for the assistant turn, optionally followed by one conversation_title
event if this was the conversation's first exchange (see GenerationWorker
._maybe_title_conversation — it deliberately arrives before the stream
closes, not after, so the sender's own connection is still open to see it).

Per CLAUDE.md's Rule #1, this module does no computation of its own — it
only persists the fast, side-effect-light setup (the user's message, a
Document stub per uploaded file, the assistant placeholder) and then
watches durable backend state. All of it happens in the plain
`post_message` handler, BEFORE any StreamingResponse is constructed:
Starlette's disconnect-driven cancellation only exists inside
StreamingResponse's own body iterator, so a plain awaited handler runs to
completion regardless of an early client disconnect.

Document ingestion (extract->chunk->embed->index) is entirely
IngestionWorker's job (src/ingestion/worker.py), on its own queue,
independent of GenerationWorker's — an embedding only ever waits for a
prior embedding, never for an unrelated LLM generation (src/concurrency.py
gives them separate locks). This route just watches: `_watch_documents`
polls Document rows directly (live for any document in the conversation,
regardless of which message attached it) and `_stream_watch` merges that
with GenerationWorker.watch()'s message events via
`merge_async_iterators`, so neither stream blocks the other. This runs
identically for a brand-new send and for a reconnect (GET
.../messages/{id}/stream) — losing this specific HTTP connection only
ever drops a *viewer* of backend state, never the state itself.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from src.api.context import AppContext, get_context
from src.api.sse import format_event, merge_async_iterators
from src.db.repository import Repository
from src.ingestion.pipeline import DocumentStatusEvent
from src.logger import get_logger
from src.models import DocumentStatus, Message
from src.rag.engine import MessageStatusEvent as RagMessageStatusEvent
from src.rag.engine import TokenEvent
from src.rag.generation_worker import ConversationTitleEvent, WorkerEvent

logger = get_logger(__name__)

router = APIRouter(prefix="/conversations", tags=["messages"])


def _sanitize_upload_filename(filename: str) -> str:
    """Reduce a client-supplied upload filename to a safe bare basename.

    The filename arrives untrusted: a crafted multipart form could set it to
    ``../../evil`` (or a name with raw CR/LF) to escape the upload directory
    via path traversal on the write path, or to inject bytes into the
    ``Content-Disposition`` header on the get-document-file route (which
    interpolates ``document.filename`` into a ``filename="..."`` header).

    ``Path(name).name`` drops any directory components/separators; what
    remains is then stripped of control characters (CR/LF/tab/NUL/…) and
    double-quotes, all of which are never part of a legit file name but
    would break HTTP-header framing or cross filesystem boundaries.
    """
    name = Path(filename).name
    cleaned = "".join(ch for ch in name if ch >= " " and ch != chr(0x7F) and ch != '"')
    # A fully-hostile name can reduce to empty; fall back so a document row
    # still exists (the download URL is keyed by document id, never the name).
    return cleaned or "upload"


@router.post("/{conversation_id}/messages")
async def post_message(
    conversation_id: str,
    prompt: str = Form(default=""),
    files: list[UploadFile] = File(default_factory=list),
    context: AppContext = Depends(get_context),
) -> StreamingResponse:
    # Validate before doing anything else — a stale conversation_id (e.g.
    # the DB was wiped but the browser still references an old URL) would
    # otherwise fail on the first Document INSERT with a FOREIGN KEY error.
    conversation = await context.repository.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # When the user drops a file without typing a question, fill in a
    # sensible default so the LLM has something to answer rather than
    # receiving an empty user message.
    prompt_text = prompt.strip()
    if not prompt_text and files:
        prompt_text = "Summarize the uploaded document."

    # Everything below is a plain awaited handler body, not a streaming
    # generator — see this module's docstring for why that matters. All of
    # it runs to completion even if the client disconnects immediately
    # after this request is sent.
    user_message = Message(
        id=str(uuid.uuid4()), conversation_id=conversation_id, role="user", content=prompt_text
    )
    await context.repository.create_message(user_message)

    # Filenames are client-supplied, so reduce each one to a bare basename
    # and strip CR/LF/control characters before it reaches the filesystem
    # (`upload_dir / "{doc_id}_{filename}"`) and the `Content-Disposition`
    # header on the download route — otherwise a crafted name could walk up
    # the upload dir (via `..`) or inject extra header bytes on the
    # get-document-file response. This mirrors the bare-basename-only
    # validation already applied to model selection in routes_models.py.
    # Read each upload's content upfront — Starlette's UploadFile is
    # spooled and may be cleaned up once this handler returns.
    for upload in files:
        content = await upload.read()
        filename = _sanitize_upload_filename(upload.filename or "upload")
        await context.pipeline.create_stub(conversation_id, filename, content)

    # Enqueued last: by the time IngestionWorker or GenerationWorker ever
    # look at this conversation, every Document stub above already exists.
    message_id = await context.generation_worker.enqueue_new(conversation_id)

    logger.info(
        "sse_send_start conversation=%s message=%s files=%d", conversation_id, message_id, len(files)
    )
    return StreamingResponse(
        _stream_watch(conversation_id, message_id, context), media_type="text/event-stream"
    )


@router.get("/{conversation_id}/messages/{message_id}/stream")
async def stream_message(
    conversation_id: str,
    message_id: str,
    context: AppContext = Depends(get_context),
) -> StreamingResponse:
    """Reconnect endpoint — what a refreshed page calls to keep watching an
    in-progress (or already-finished) generation it didn't originate."""
    message = await context.repository.get_message(message_id)
    if message is None or message.conversation_id != conversation_id:
        raise HTTPException(status_code=404, detail="Message not found in this conversation")
    return StreamingResponse(
        _stream_watch(conversation_id, message_id, context), media_type="text/event-stream"
    )


async def _stream_watch(conversation_id: str, message_id: str, context: AppContext) -> AsyncIterator[str]:
    """The one streaming body for both a brand-new send and a reconnect —
    purely a watcher, doing no work of its own. Merges document-status
    watching (independent of GenerationWorker, see _watch_documents below)
    with GenerationWorker.watch()'s message events, so neither blocks the
    other."""
    logger.info("sse_watch_start conversation=%s message=%s", conversation_id, message_id)
    try:
        async for event in merge_async_iterators(
            _watch_documents(conversation_id, context.repository),
            context.generation_worker.watch(message_id),
        ):
            yield _format_rag_event(event)
    finally:
        logger.info("sse_watch_end conversation=%s message=%s", conversation_id, message_id)


async def _watch_documents(conversation_id: str, repository: Repository) -> AsyncIterator[DocumentStatusEvent]:
    """Poll this conversation's Document rows directly and yield on every
    status transition, until none are non-terminal. Independent of
    IngestionWorker (which does the actual work) and of GenerationWorker
    (which only waits on this same state, never reports it) — this is the
    sole source of document_status events, live or on reconnect alike, so
    a conversation's Sources panel never freezes behind some other
    conversation's LLM generation.
    """
    terminal = frozenset({DocumentStatus.INDEXED, DocumentStatus.ERROR, DocumentStatus.UNSUPPORTED})
    last_seen: dict[str, DocumentStatus] = {}
    while True:
        docs = await repository.list_documents(conversation_id)
        pending = False
        for doc in docs:
            if last_seen.get(doc.id) != doc.status:
                last_seen[doc.id] = doc.status
                yield DocumentStatusEvent(doc.id, doc.status, doc.error_message)
            if doc.status not in terminal:
                pending = True
        if not pending:
            return
        await asyncio.sleep(0.2)


def _format_rag_event(event: WorkerEvent | DocumentStatusEvent) -> str:
    if isinstance(event, TokenEvent):
        return format_event("token", {"text": event.text})
    if isinstance(event, RagMessageStatusEvent):
        return format_event(
            "message_status",
            {
                "message_id": event.message_id,
                "status": event.status.value,
                "error_message": event.error_message,
            },
        )
    if isinstance(event, ConversationTitleEvent):
        return format_event(
            "conversation_title",
            {"conversation_id": event.conversation_id, "title": event.title},
        )
    if isinstance(event, DocumentStatusEvent):
        return format_event(
            "document_status",
            {
                "document_id": event.document_id,
                "status": event.status.value,
                "error_message": event.error_message,
            },
        )
    # DoneEvent (the only remaining WorkerEvent variant)
    citations = [citation.model_dump(mode="json") for citation in event.message.citations]
    return "".join(
        [
            format_event("citations", {"citations": citations}),
            format_event(
                "done",
                {
                    "message_id": event.message.id,
                    "content": event.message.content,
                    "status": event.message.status.value,
                    "error_message": event.message.error_message,
                },
            ),
        ]
    )
