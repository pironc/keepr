"""Repository: the only module that speaks SQL.

Everything above this layer works with the Pydantic models from
`src.models`, never raw rows — which is exactly what makes a future
SQLite -> Postgres swap (see ARCHITECTURE.md) contained to this one file.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import Field, TypeAdapter

from src.db.pool import SQLiteConnectionPool
from src.db.schema import SCHEMA_STATEMENTS
from src.models import (
    Chunk,
    Citation,
    Conversation,
    Document,
    DocumentStatus,
    Message,
    MessageStatus,
    SourceKind,
    SourceRef,
)

_source_ref_adapter: TypeAdapter[SourceRef] = TypeAdapter(Annotated[SourceRef, Field(discriminator="kind")])
_citations_adapter: TypeAdapter[list[Citation]] = TypeAdapter(list[Citation])


class Repository:
    def __init__(self, pool: SQLiteConnectionPool) -> None:
        self._pool = pool

    async def initialize(self) -> None:
        async with self._pool.acquire() as connection:
            for statement in SCHEMA_STATEMENTS:
                # ALTER TABLE ADD COLUMN fails if the column already exists
                # and SQLite has no IF NOT EXISTS for it — swallow that
                # specific case so a pre-existing database starts cleanly.
                try:
                    await connection.execute(statement)
                except Exception:
                    if not statement.lstrip().upper().startswith("ALTER TABLE"):
                        raise
            await connection.commit()

    # -- conversations --------------------------------------------------

    async def create_conversation(self, conversation: Conversation) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO conversations (id, title, pinned, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (
                    conversation.id,
                    conversation.title,
                    int(conversation.pinned),
                    conversation.created_at.isoformat(),
                    conversation.updated_at.isoformat(),
                ),
            )
            await connection.commit()

    async def list_conversations(self) -> list[Conversation]:
        async with self._pool.acquire() as connection:
            cursor = await connection.execute(
                "SELECT id, title, pinned, created_at, updated_at FROM conversations ORDER BY pinned DESC, updated_at DESC"
            )
            rows = await cursor.fetchall()
        return [_conversation_from_row(row) for row in rows]

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        async with self._pool.acquire() as connection:
            cursor = await connection.execute(
                "SELECT id, title, pinned, created_at, updated_at FROM conversations WHERE id = ?",
                (conversation_id,),
            )
            row = await cursor.fetchone()
        return _conversation_from_row(row) if row is not None else None

    async def touch_conversation(self, conversation_id: str) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(), conversation_id),
            )
            await connection.commit()

    async def update_conversation_title(self, conversation_id: str, title: str) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                "UPDATE conversations SET title = ? WHERE id = ?",
                (title, conversation_id),
            )
            await connection.commit()

    # -- documents --------------------------------------------------

    async def create_document(self, document: Document) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO documents
                    (id, conversation_id, filename, source_kind, content_hash, status, error_message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document.id,
                    document.conversation_id,
                    document.filename,
                    document.source_kind.value,
                    document.content_hash,
                    document.status.value,
                    document.error_message,
                    document.created_at.isoformat(),
                ),
            )
            await connection.commit()

    async def get_document_by_content_hash(
        self, conversation_id: str, content_hash: str
    ) -> Document | None:
        async with self._pool.acquire() as connection:
            cursor = await connection.execute(
                """
                SELECT id, conversation_id, filename, source_kind, content_hash, status, error_message, created_at
                FROM documents WHERE conversation_id = ? AND content_hash = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (conversation_id, content_hash),
            )
            row = await cursor.fetchone()
        return _document_from_row(row) if row is not None else None

    async def update_document_status(
        self, document_id: str, status: DocumentStatus, error_message: str | None = None
    ) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                "UPDATE documents SET status = ?, error_message = ? WHERE id = ?",
                (status.value, error_message, document_id),
            )
            await connection.commit()

    async def list_documents(self, conversation_id: str) -> list[Document]:
        async with self._pool.acquire() as connection:
            cursor = await connection.execute(
                """
                SELECT id, conversation_id, filename, source_kind, content_hash, status, error_message, created_at
                FROM documents WHERE conversation_id = ? ORDER BY created_at ASC
                """,
                (conversation_id,),
            )
            rows = await cursor.fetchall()
        return [_document_from_row(row) for row in rows]

    async def get_document(self, document_id: str) -> Document | None:
        async with self._pool.acquire() as connection:
            cursor = await connection.execute(
                """
                SELECT id, conversation_id, filename, source_kind, content_hash, status, error_message, created_at
                FROM documents WHERE id = ?
                """,
                (document_id,),
            )
            row = await cursor.fetchone()
        return _document_from_row(row) if row is not None else None

    # -- chunks --------------------------------------------------

    async def create_chunks(self, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        async with self._pool.acquire() as connection:
            # Wrap the entire batch in a single transaction — without this,
            # each INSERT auto-commits separately and triggers its own fsync,
            # which for large documents (hundreds of chunks) keeps the write
            # lock held near-continuously for seconds.  Use BEGIN IMMEDIATE
            # so the writer acquires the lock upfront rather than doing all
            # the work and then failing at COMMIT with SQLITE_BUSY.
            #
            # Important: aiosqlite uses isolation_level='' (autocommit mode),
            # so connection.commit() is a no-op.  We must use execute("COMMIT")
            # to actually finalise an explicit transaction.  Likewise, a
            # failure between BEGIN and COMMIT would leak the RESERVED lock
            # back into the pool forever — we ROLLBACK on any error to
            # prevent that.
            await connection.execute("BEGIN IMMEDIATE")
            try:
                await connection.executemany(
                    """
                    INSERT INTO chunks (id, document_id, conversation_id, chunk_index, text, source_ref_json)
                    VALUES (:id, :document_id, :conversation_id, :chunk_index, :text, :source_ref_json)
                    """,
                    [
                        {
                            "id": chunk.id,
                            "document_id": chunk.document_id,
                            "conversation_id": chunk.conversation_id,
                            "chunk_index": chunk.chunk_index,
                            "text": chunk.text,
                            "source_ref_json": chunk.source_ref.model_dump_json(),
                        }
                        for chunk in chunks
                    ],
                )
            except Exception:
                await connection.execute("ROLLBACK")
                raise
            await connection.execute("COMMIT")

    async def get_chunks_by_ids(self, chunk_ids: list[str]) -> list[Chunk]:
        if not chunk_ids:
            return []
        placeholders = ",".join("?" for _ in chunk_ids)
        async with self._pool.acquire() as connection:
            cursor = await connection.execute(
                f"""
                SELECT id, document_id, conversation_id, chunk_index, text, source_ref_json
                FROM chunks WHERE id IN ({placeholders})
                """,
                chunk_ids,
            )
            rows = await cursor.fetchall()
        by_id = {row["id"]: _chunk_from_row(row) for row in rows}
        return [by_id[chunk_id] for chunk_id in chunk_ids if chunk_id in by_id]

    async def list_chunks(self, conversation_id: str) -> list[Chunk]:
        async with self._pool.acquire() as connection:
            cursor = await connection.execute(
                """
                SELECT id, document_id, conversation_id, chunk_index, text, source_ref_json
                FROM chunks WHERE conversation_id = ? ORDER BY document_id, chunk_index
                """,
                (conversation_id,),
            )
            rows = await cursor.fetchall()
        return [_chunk_from_row(row) for row in rows]

    # -- messages --------------------------------------------------

    async def create_message(self, message: Message) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO messages
                    (id, conversation_id, role, content, citations_json, status, error_message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.id,
                    message.conversation_id,
                    message.role,
                    message.content,
                    _citations_adapter.dump_json(message.citations).decode("utf-8"),
                    message.status.value,
                    message.error_message,
                    message.created_at.isoformat(),
                ),
            )
            await connection.commit()

    async def list_messages(self, conversation_id: str) -> list[Message]:
        async with self._pool.acquire() as connection:
            cursor = await connection.execute(
                """
                SELECT id, conversation_id, role, content, citations_json, status, error_message, created_at
                FROM messages WHERE conversation_id = ? ORDER BY created_at ASC, rowid ASC
                """,
                (conversation_id,),
            )
            rows = await cursor.fetchall()
        return [_message_from_row(row) for row in rows]

    async def get_message(self, message_id: str) -> Message | None:
        async with self._pool.acquire() as connection:
            cursor = await connection.execute(
                """
                SELECT id, conversation_id, role, content, citations_json, status, error_message, created_at
                FROM messages WHERE id = ?
                """,
                (message_id,),
            )
            row = await cursor.fetchone()
        return _message_from_row(row) if row is not None else None

    async def update_message_status(
        self, message_id: str, status: MessageStatus, error_message: str | None = None
    ) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                "UPDATE messages SET status = ?, error_message = ? WHERE id = ?",
                (status.value, error_message, message_id),
            )
            await connection.commit()

    async def finalize_message(
        self,
        message_id: str,
        status: MessageStatus,
        content: str,
        citations: list[Citation],
        error_message: str | None = None,
    ) -> None:
        # Deliberately never touches created_at — a slower-but-first-asked
        # message must stay ordered ahead of a faster-but-later one
        # regardless of which one's generation actually finishes first.
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE messages
                SET content = ?, citations_json = ?, status = ?, error_message = ?
                WHERE id = ?
                """,
                (
                    content,
                    _citations_adapter.dump_json(citations).decode("utf-8"),
                    status.value,
                    error_message,
                    message_id,
                ),
            )
            await connection.commit()

    async def get_oldest_queued_message(self) -> Message | None:
        # Unscoped by conversation_id on purpose: there is one GenerationWorker,
        # one queue, app-wide (one shared LLM instance behind it).
        async with self._pool.acquire() as connection:
            cursor = await connection.execute(
                """
                SELECT id, conversation_id, role, content, citations_json, status, error_message, created_at
                FROM messages WHERE status = ?
                ORDER BY created_at ASC, rowid ASC LIMIT 1
                """,
                (MessageStatus.QUEUED.value,),
            )
            row = await cursor.fetchone()
        return _message_from_row(row) if row is not None else None

    async def list_nonterminal_messages(self) -> list[Message]:
        async with self._pool.acquire() as connection:
            cursor = await connection.execute(
                """
                SELECT id, conversation_id, role, content, citations_json, status, error_message, created_at
                FROM messages WHERE status != ? AND status != ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (MessageStatus.DONE.value, MessageStatus.ERROR.value),
            )
            rows = await cursor.fetchall()
        return [_message_from_row(row) for row in rows]


    async def update_conversation_pinned(self, conversation_id: str, pinned: bool) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                "UPDATE conversations SET pinned = ? WHERE id = ?",
                (int(pinned), conversation_id),
            )
            await connection.commit()

    async def list_document_ids(self, conversation_id: str) -> list[tuple[str, str]]:
        """Return (document_id, filename) pairs for every document in a conversation."""
        async with self._pool.acquire() as connection:
            cursor = await connection.execute(
                "SELECT id, filename FROM documents WHERE conversation_id = ?",
                (conversation_id,),
            )
            rows = await cursor.fetchall()
        return [(row["id"], row["filename"]) for row in rows]

    async def delete_conversation(self, conversation_id: str) -> None:
        """Delete a conversation and all its dependent rows.

        SQLite foreign keys are not enforced by default, so children are
        deleted explicitly before the parent, in dependency order."""
        async with self._pool.acquire() as connection:
            await connection.execute(
                "DELETE FROM messages WHERE conversation_id = ?", (conversation_id,)
            )
            await connection.execute(
                "DELETE FROM chunks WHERE conversation_id = ?", (conversation_id,)
            )
            await connection.execute(
                "DELETE FROM documents WHERE conversation_id = ?", (conversation_id,)
            )
            await connection.execute(
                "DELETE FROM conversations WHERE id = ?", (conversation_id,)
            )
            await connection.commit()


def _conversation_from_row(row: Any) -> Conversation:
    pinned = bool(row["pinned"]) if "pinned" in row else False
    return Conversation(
        id=row["id"], title=row["title"], pinned=pinned, created_at=row["created_at"], updated_at=row["updated_at"]
    )


def _document_from_row(row: Any) -> Document:
    return Document(
        id=row["id"],
        conversation_id=row["conversation_id"],
        filename=row["filename"],
        source_kind=SourceKind(row["source_kind"]),
        content_hash=row["content_hash"],
        status=DocumentStatus(row["status"]),
        error_message=row["error_message"],
        created_at=row["created_at"],
    )


def _chunk_from_row(row: Any) -> Chunk:
    return Chunk(
        id=row["id"],
        document_id=row["document_id"],
        conversation_id=row["conversation_id"],
        chunk_index=row["chunk_index"],
        text=row["text"],
        source_ref=_source_ref_adapter.validate_json(row["source_ref_json"]),
    )


def _message_from_row(row: Any) -> Message:
    return Message(
        id=row["id"],
        conversation_id=row["conversation_id"],
        role=row["role"],
        content=row["content"],
        citations=_citations_adapter.validate_json(row["citations_json"]),
        status=MessageStatus(row["status"]),
        error_message=row["error_message"],
        created_at=row["created_at"],
    )
