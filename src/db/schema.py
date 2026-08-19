"""SQL schema.

SQLite is the right storage engine for a single local user on a laptop.
The scale-up path (see ARCHITECTURE.md) is a straight swap to Postgres
behind this same module's `Repository` interface — nothing above this
layer would need to change.
"""

from __future__ import annotations

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        pinned INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # Separate from CREATE TABLE so new databases get the column and existing
    # ones are migrated in-place — SQLite has no ADD COLUMN IF NOT EXISTS,
    # and this project deliberately avoids a migrations framework (CLAUDE.md).
    "ALTER TABLE conversations ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
    """
    CREATE TABLE IF NOT EXISTS documents (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id),
        filename TEXT NOT NULL,
        source_kind TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        error_message TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chunks (
        id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES documents(id),
        conversation_id TEXT NOT NULL REFERENCES conversations(id),
        chunk_index INTEGER NOT NULL,
        text TEXT NOT NULL,
        source_ref_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id),
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        citations_json TEXT NOT NULL,
        status TEXT NOT NULL,
        error_message TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_documents_conversation ON documents(conversation_id)",
    "CREATE INDEX IF NOT EXISTS idx_documents_content_hash ON documents(conversation_id, content_hash)",
    "CREATE INDEX IF NOT EXISTS idx_chunks_conversation ON chunks(conversation_id)",
    "CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id)",
    # GenerationWorker queries across ALL conversations by status (there is
    # one worker, one queue, app-wide) — not scoped to a single
    # conversation_id, so it needs its own index rather than reusing the one above.
    "CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status)",
    # Same reasoning as idx_messages_status: IngestionWorker queries across
    # ALL conversations by status (one worker, one queue, app-wide).
    "CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status)",
)
