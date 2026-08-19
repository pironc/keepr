"""Direct tests for Repository's transaction-safety in create_chunks — the
one method that manages an explicit transaction by hand (BEGIN IMMEDIATE /
COMMIT / ROLLBACK) rather than relying on aiosqlite's autocommit mode, since
a batch of chunk inserts needs to be one atomic unit (see the method's own
comment in src/db/repository.py)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest

from src.db.pool import SQLiteConnectionPool
from src.db.repository import Repository
from src.models import (
    Chunk,
    Conversation,
    Document,
    DocumentStatus,
    Message,
    MessageStatus,
    PageRef,
    SourceKind,
)


async def test_create_chunks_commit_failure_does_not_leave_the_lock_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = SQLiteConnectionPool(tmp_path / "test.db", pool_size=2)
    repository = Repository(pool)
    await repository.initialize()

    await repository.create_conversation(Conversation(id="conv-1", title="test"))
    document = Document(
        id="doc-1",
        conversation_id="conv-1",
        filename="manual.pdf",
        source_kind=SourceKind.PDF,
        content_hash="test-hash",
        status=DocumentStatus.INDEXED,
    )
    await repository.create_document(document)
    chunk = Chunk(
        id="chunk-1",
        document_id=document.id,
        conversation_id="conv-1",
        text="hello",
        source_ref=PageRef(page=1),
        chunk_index=0,
    )

    real_execute = aiosqlite.Connection.execute

    async def flaky_execute(
        self: aiosqlite.Connection, sql: str, parameters: object = None
    ) -> aiosqlite.Cursor:
        if isinstance(sql, str) and sql.strip() == "COMMIT":
            raise aiosqlite.OperationalError("simulated commit failure")
        return await real_execute(self, sql, parameters)

    monkeypatch.setattr(aiosqlite.Connection, "execute", flaky_execute)

    with pytest.raises(Exception):  # noqa: B017 - simulating a real sqlite3.OperationalError
        await repository.create_chunks([chunk])

    monkeypatch.setattr(aiosqlite.Connection, "execute", real_execute)

    # A completely unrelated write, on whatever connection the pool hands
    # back next, must not be blocked by the failed create_chunks call above.
    # If create_chunks' BEGIN IMMEDIATE transaction was left open (COMMIT
    # failed and nothing rolled it back), this hangs until busy_timeout
    # gives up and raises "database is locked" — exactly the bug this test
    # guards against, reproduced with a real second connection from the
    # same pool rather than asserted in the abstract.
    other_document = Document(
        id="doc-2",
        conversation_id="conv-1",
        filename="other.pdf",
        source_kind=SourceKind.PDF,
        content_hash="test-hash-2",
        status=DocumentStatus.INDEXED,
    )
    await asyncio.wait_for(repository.create_document(other_document), timeout=1.0)

    await pool.close()


async def test_finalize_message_survives_a_transient_lock_without_losing_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test protecting finalize_message — the write that
    persists the final answer — against a transient "database is locked"
    (sustained write contention from a second conversation's own
    ingestion/generation outlasting the pool's 5s busy_timeout): without a
    retry, the real, already-computed content never reaches the database,
    and the worker's own failure handling then records an empty-content
    ERROR instead. Simulates the lock persisting for a couple of attempts
    before clearing, and asserts the REAL content ends up persisted — not
    silently replaced by nothing."""
    pool = SQLiteConnectionPool(tmp_path / "test.db", pool_size=2)
    repository = Repository(pool)
    await repository.initialize()

    await repository.create_conversation(Conversation(id="conv-1", title="test"))
    message = Message(
        id="msg-1", conversation_id="conv-1", role="assistant", content="",
        status=MessageStatus.GENERATING,
    )
    await repository.create_message(message)

    real_execute = aiosqlite.Connection.execute
    calls = 0

    async def flaky_execute(
        self: aiosqlite.Connection, sql: str, parameters: object = None
    ) -> aiosqlite.Cursor:
        nonlocal calls
        if isinstance(sql, str) and "citations_json" in sql:
            calls += 1
            if calls <= 2:
                raise aiosqlite.OperationalError("simulated database is locked")
        return await real_execute(self, sql, parameters)

    monkeypatch.setattr(aiosqlite.Connection, "execute", flaky_execute)

    real_answer = "This is the fully-generated answer the user actually saw stream in."
    await repository.finalize_message("msg-1", MessageStatus.DONE, real_answer, [])

    monkeypatch.setattr(aiosqlite.Connection, "execute", real_execute)
    assert calls == 3, "expected two failed attempts, then a third that succeeded"

    persisted = await repository.get_message("msg-1")
    assert persisted is not None
    assert persisted.status == MessageStatus.DONE
    assert persisted.content == real_answer, "the real answer must survive, not be lost or blanked"

    await pool.close()
