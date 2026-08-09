"""POST /conversations/{id}/messages — accepts a prompt plus optional file
attachments, and streams back a single multiplexed SSE response:
document_status events (drives the UI's per-file processing animation)
for any newly attached files, then message_status/token/citations/done
events for the assistant turn, optionally followed by one conversation_title
event if this was the conversation's first exchange (see GenerationWorker
._maybe_title_conversation — it deliberately arrives before the stream
closes, not after, so the sender's own connection is still open to see it).

The assistant placeholder is enqueued BEFORE file ingestion runs, so that
even if the client disconnects mid-ingestion (page refresh, navigation)
the QUEUED row already exists and GenerationWorker will pick it up.  Its
_ensure_documents_indexed fallback then finishes any document that never
reached INDEXED — the pipeline's content-hash dedup prevents re-creating
the document row, and the worker re-runs the full pipeline from the saved
file bytes.  This is what prevents the "Embedding… forever" bug when a
user sends a file and leaves before ingestion completes.

File ingestion runs in this SSE generator (the primary path) so that
documents go through the full pipeline immediately — even while another
chat is mid-generation.  This is safe because the embedder and LLM driver
are protected by separate asyncio.Lock instances (LockedEmbedder vs
LockedLLMDriver in src/concurrency.py): embedding for Chat B's newly
dropped file does not compete with Chat A's LLM inference, and the SQLite
busy_timeout absorbs concurrent writes from the two paths.

Generation itself runs on GenerationWorker, independent of this specific
HTTP connection's lifetime — this route only subscribes to it via
watch(). If the client disconnects mid-generation, only this subscriber
dies; the worker keeps running and keeps persisting to the DB regardless.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from src.api.context import AppContext, get_context
from src.api.sse import format_event
from src.ingestion.pipeline import DocumentStatusEvent
from src.logger import get_logger
from src.models import Message
from src.rag.engine import MessageStatusEvent as RagMessageStatusEvent
from src.rag.engine import TokenEvent
from src.rag.generation_worker import ConversationTitleEvent, WorkerEvent

logger = get_logger(__name__)

router = APIRouter(prefix="/conversations", tags=["messages"])


@router.post("/{conversation_id}/messages")
async def post_message(
    conversation_id: str,
    prompt: str = Form(default=""),
    files: list[UploadFile] = File(default_factory=list),
    context: AppContext = Depends(get_context),
) -> StreamingResponse:
    # Validate before streaming — a stale conversation_id (e.g. the DB was
    # wiped but the browser still references an old URL) would otherwise
    # crash mid-stream on the first document INSERT with a FOREIGN KEY error.
    conversation = await context.repository.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # When the user drops a file without typing a question, fill in a
    # sensible default so the LLM has something to answer rather than
    # receiving an empty user message.
    prompt_text = prompt.strip()
    if not prompt_text and files:
        prompt_text = "Summarize the uploaded document."

    return StreamingResponse(
        _stream_new_message(conversation_id, prompt_text, files, context),
        media_type="text/event-stream",
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
        _stream_watch(message_id, context), media_type="text/event-stream"
    )


async def _stream_new_message(
    conversation_id: str,
    prompt: str,
    files: list[UploadFile],
    context: AppContext,
) -> AsyncIterator[str]:
    logger.info("sse_send_start conversation=%s files=%d", conversation_id, len(files))

    try:
        # 1. Persist the user message — even if the client disconnects before
        #    the first SSE event is sent, the question is already saved.
        user_message = Message(
            id=str(uuid.uuid4()), conversation_id=conversation_id, role="user", content=prompt
        )
        await context.repository.create_message(user_message)

        # 2. Read all file content upfront (Starlette's UploadFile is spooled
        #    and may be cleaned up after the request handler returns).
        file_payloads: list[tuple[str, str, bytes]] = []
        for upload in files:
            content = await upload.read()
            filename = upload.filename or "upload"
            mime_type = upload.content_type or "application/octet-stream"
            file_payloads.append((filename, mime_type, content))

        # 3. Enqueue the assistant placeholder BEFORE ingestion.  If the
        #    client disconnects during the ingestion loop below (page
        #    refresh, navigation), the QUEUED row already exists and the
        #    worker will pick it up — its _ensure_documents_indexed fallback
        #    finishes any document that never reached INDEXED.  If ingestion
        #    completes first (the common case), the worker's fallback is a
        #    no-op (documents are already INDEXED).
        message_id = await context.generation_worker.enqueue_new(conversation_id)

        # 4. Run ingestion NOW — extract, chunk, embed, and index every
        #    attached file immediately.  This is the primary path, and it
        #    runs fine even while another conversation's LLM is generating:
        #    LockedEmbedder and LockedLLMDriver use separate asyncio.Lock
        #    instances, so embedding for ingestion does NOT compete with
        #    the other chat's inference.
        #
        #    Note: enqueue_new above created a QUEUED row for this
        #    conversation.  If ingestion takes > 500ms (large file), the
        #    worker may pick up that QUEUED job while this generator is
        #    still ingesting.  The worker's _ensure_documents_indexed will
        #    then find the document still non-INDEXED and attempt to
        #    re-ingest it concurrently — the pipeline's content-hash dedup
        #    prevents a duplicate document row, but the two parallel
        #    ingest() runs can create duplicate chunks in the index.  This
        #    race window is narrow (ingestion of typical files completes
        #    within the worker's 500ms poll interval) and the consequence
        #    is duplicate citations rather than data loss; a future staleness
        #    check (using document updated_at) would close it entirely.
        if file_payloads:
            index = await context.index_manager.get(conversation_id)
            for filename, mime_type, content in file_payloads:
                async for status_event in context.pipeline.ingest(
                    conversation_id, filename, mime_type, content, index
                ):
                    yield format_event(
                        "document_status",
                        {
                            "document_id": status_event.document_id,
                            "status": status_event.status.value,
                            "error_message": status_event.error_message,
                        },
                    )
            await context.index_manager.save(conversation_id)

        # 5. Watch the worker for RAG retrieval + LLM generation.  Document
        #    status events may also arrive here (the worker's fallback
        #    _ensure_documents_indexed) if ingestion was interrupted above.
        async for event in context.generation_worker.watch(message_id):
            yield _format_rag_event(event)
    finally:
        logger.info("sse_send_end conversation=%s", conversation_id)


async def _stream_watch(message_id: str, context: AppContext) -> AsyncIterator[str]:
    logger.info("sse_reconnect_start message_id=%s", message_id)
    try:
        async for event in context.generation_worker.watch(message_id):
            yield _format_rag_event(event)
    finally:
        logger.info("sse_reconnect_end message_id=%s", message_id)


def _format_rag_event(event: WorkerEvent) -> str:
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
