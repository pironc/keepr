"""Conversation CRUD + per-conversation document listing."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src.api.context import AppContext, get_context
from src.models import DEFAULT_CONVERSATION_TITLE, Conversation, Document, Message

router = APIRouter(prefix="/conversations", tags=["conversations"])


class UpdateConversationRequest(BaseModel):
    title: str | None = None
    pinned: bool | None = None


@router.post("")
async def create_conversation(context: AppContext = Depends(get_context)) -> Conversation:
    conversation = Conversation(id=str(uuid.uuid4()), title=DEFAULT_CONVERSATION_TITLE)
    await context.repository.create_conversation(conversation)
    return conversation


@router.get("")
async def list_conversations(context: AppContext = Depends(get_context)) -> list[Conversation]:
    return await context.repository.list_conversations()


@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: str, context: AppContext = Depends(get_context)
) -> Conversation:
    conversation = await context.repository.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@router.get("/{conversation_id}/messages")
async def list_messages(
    conversation_id: str, context: AppContext = Depends(get_context)
) -> list[Message]:
    return await context.repository.list_messages(conversation_id)


@router.get("/{conversation_id}/documents")
async def list_documents(
    conversation_id: str, context: AppContext = Depends(get_context)
) -> list[Document]:
    return await context.repository.list_documents(conversation_id)


@router.patch("/{conversation_id}")
async def update_conversation(
    conversation_id: str,
    body: UpdateConversationRequest,
    context: AppContext = Depends(get_context),
) -> Conversation:
    conversation = await context.repository.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=422, detail="Title must not be empty")
        await context.repository.update_conversation_title(conversation_id, title)

    if body.pinned is not None:
        await context.repository.update_conversation_pinned(conversation_id, body.pinned)

    updated = await context.repository.get_conversation(conversation_id)
    assert updated is not None
    return updated


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: str, context: AppContext = Depends(get_context)
) -> None:
    conversation = await context.repository.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Gather document paths for filesystem cleanup before removing DB rows.
    doc_pairs = await context.repository.list_document_ids(conversation_id)

    await context.repository.delete_conversation(conversation_id)

    # Clean up the vector index for this conversation.
    index_path = Path(context.settings.index_dir) / f"{conversation_id}.npz"
    await asyncio.to_thread(_remove_if_exists, index_path)

    # Clean up uploaded files.
    for doc_id, filename in doc_pairs:
        file_path = Path(context.settings.upload_dir) / f"{doc_id}_{filename}"
        await asyncio.to_thread(_remove_if_exists, file_path)


def _remove_if_exists(path: Path) -> None:
    import contextlib

    with contextlib.suppress(FileNotFoundError):
        path.unlink()


@router.get("/{conversation_id}/documents/{document_id}/file")
async def get_document_file(
    conversation_id: str,
    document_id: str,
    context: AppContext = Depends(get_context),
) -> FileResponse:
    document = await context.repository.get_document(document_id)
    if document is None or document.conversation_id != conversation_id:
        raise HTTPException(status_code=404, detail="Document not found in this conversation")

    file_path = Path(context.settings.upload_dir) / f"{document_id}_{document.filename}"
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found on disk")

    media_type = _media_type_for(document.filename)
    return FileResponse(
        file_path,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{document.filename}"'},
    )


def _media_type_for(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith(".txt"):
        return "text/plain"
    if lower.endswith(".md"):
        return "text/markdown"
    return "application/octet-stream"
