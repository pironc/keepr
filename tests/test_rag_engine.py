"""Tests for RagEngine: confidence-gated refusal and citation-ID verification.

These are the two properties this project's "anti-hallucination" claim
actually rests on — both are deterministic and asserted here, not just
described in a prompt.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from src.db.repository import Repository
from src.embeddings.mock_embedder import MockEmbedder
from src.llm.base import LLMDriver
from src.llm.mock_driver import MockLLMDriver
from src.models import (
    Chunk,
    Conversation,
    Document,
    DocumentStatus,
    LLMMessage,
    PageRef,
    SourceKind,
)
from src.rag.engine import DoneEvent, RagEngine, TokenEvent
from src.rag.greeting import _FAREWELL_RESPONSE, _GREETING_RESPONSE
from src.rag.prompts import REFUSAL_TEXT
from src.vectorstore.flat_index import NumpyFlatIndex


class _FabricatingDriver(LLMDriver):
    """A driver that cites a chunk ID that was never actually retrieved."""

    async def generate(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        yield "According to the manual [chunk_1] and also [chunk_99], which was never retrieved."


async def _seed_chunk(repository: Repository, conversation_id: str, text: str) -> Chunk:
    await repository.create_conversation(Conversation(id=conversation_id, title="test"))
    document = Document(
        id="doc-1",
        conversation_id=conversation_id,
        filename="manual.pdf",
        source_kind=SourceKind.PDF,
        content_hash="test-hash",
        status=DocumentStatus.INDEXED,
    )
    await repository.create_document(document)
    chunk = Chunk(
        id="chunk-1",
        document_id=document.id,
        conversation_id=conversation_id,
        text=text,
        source_ref=PageRef(page=1),
        chunk_index=0,
    )
    await repository.create_chunks([chunk])
    return chunk


async def test_refuses_before_calling_the_llm_when_index_is_empty(
    repository: Repository, embedder: MockEmbedder, llm_driver: MockLLMDriver
) -> None:
    await repository.create_conversation(Conversation(id="conv-1", title="test"))
    engine = RagEngine(repository=repository, embedder=embedder, top_k=5, min_similarity=0.35)
    index = NumpyFlatIndex()

    events = [
        event
        async for event in engine.answer(
            "conv-1", "What is the drone's max flight time?", [], index, llm_driver
        )
    ]

    assert len(events) == 1
    assert isinstance(events[0], DoneEvent)
    assert events[0].message.content == REFUSAL_TEXT
    assert events[0].message.citations == []


async def test_falls_back_to_top_chunks_when_nothing_clears_the_similarity_bar(
    repository: Repository, embedder: MockEmbedder, llm_driver: MockLLMDriver
) -> None:
    """When the index is non-empty but every chunk is below the similarity
    threshold (e.g. a general synopsis question asked against the mock
    bag-of-words embedder, which has zero semantic understanding), the
    engine falls back to the top-scoring chunks rather than refusing
    outright — the LLM's own prompt instructions still handle the case
    where those chunks genuinely don't answer the question."""
    conversation_id = "conv-2"
    chunk = await _seed_chunk(
        repository, conversation_id, "The drone's max flight time is 28 minutes on a full battery."
    )
    index = NumpyFlatIndex()
    index.add([chunk.id], await embedder.embed_documents([chunk.text]))

    engine = RagEngine(repository=repository, embedder=embedder, top_k=5, min_similarity=0.99)

    events = [
        event
        async for event in engine.answer(
            conversation_id, "completely unrelated question about tax law", [], index, llm_driver
        )
    ]

    done = events[-1]
    assert isinstance(done, DoneEvent)
    # With the fallback, we get an answer rather than a refusal — the LLM
    # still sees real document chunks and its own prompt instructions tell
    # it to refuse if the chunks don't actually answer the question.
    assert done.message.content != REFUSAL_TEXT


async def test_answers_with_citation_when_retrieval_clears_the_bar(
    repository: Repository, embedder: MockEmbedder, llm_driver: MockLLMDriver
) -> None:
    conversation_id = "conv-3"
    chunk = await _seed_chunk(
        repository, conversation_id, "The drone's max flight time is 28 minutes on a full battery."
    )
    index = NumpyFlatIndex()
    index.add([chunk.id], await embedder.embed_documents([chunk.text]))

    engine = RagEngine(repository=repository, embedder=embedder, top_k=5, min_similarity=0.0)

    events = [
        event
        async for event in engine.answer(
            conversation_id, "drone max flight time battery", [], index, llm_driver
        )
    ]

    tokens = "".join(event.text for event in events if isinstance(event, TokenEvent))
    assert tokens.strip() != ""

    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert len(done.message.citations) == 1
    assert done.message.citations[0].chunk_id == chunk.id
    assert done.message.citations[0].document_filename == "manual.pdf"


async def test_low_scoring_chunks_are_dropped_even_when_the_top_chunk_clears_the_bar(
    repository: Repository, embedder: MockEmbedder, llm_driver: MockLLMDriver
) -> None:
    conversation_id = "conv-5"
    await repository.create_conversation(Conversation(id=conversation_id, title="test"))
    document = Document(
        id="doc-5",
        conversation_id=conversation_id,
        filename="manual.pdf",
        source_kind=SourceKind.PDF,
        content_hash="test-hash",
        status=DocumentStatus.INDEXED,
    )
    await repository.create_document(document)
    relevant = Chunk(
        id="chunk-relevant",
        document_id=document.id,
        conversation_id=conversation_id,
        text="The quantized vector index cuts memory usage by four times.",
        source_ref=PageRef(page=1),
        chunk_index=0,
    )
    irrelevant = Chunk(
        id="chunk-irrelevant",
        document_id=document.id,
        conversation_id=conversation_id,
        text="The weather today is sunny with clear skies over the mountains.",
        source_ref=PageRef(page=2),
        chunk_index=1,
    )
    await repository.create_chunks([relevant, irrelevant])

    index = NumpyFlatIndex()
    index.add(
        [relevant.id, irrelevant.id],
        await embedder.embed_documents([relevant.text, irrelevant.text]),
    )

    # Measured: "relevant" scores ~0.41, "irrelevant" scores ~0.29 against
    # this query — 0.35 sits cleanly between them, so both make the top-k
    # cut but only one should actually qualify.
    engine = RagEngine(repository=repository, embedder=embedder, top_k=2, min_similarity=0.35)

    events = [
        event
        async for event in engine.answer(
            conversation_id, "how much memory does the quantized index save", [], index, llm_driver
        )
    ]

    done = events[-1]
    assert isinstance(done, DoneEvent)
    cited_ids = {citation.chunk_id for citation in done.message.citations}
    assert cited_ids == {relevant.id}


async def test_citation_tags_are_stripped_from_display_text(
    repository: Repository, embedder: MockEmbedder, llm_driver: MockLLMDriver
) -> None:
    conversation_id = "conv-6"
    await repository.create_conversation(Conversation(id=conversation_id, title="test"))
    document = Document(
        id="doc-6",
        conversation_id=conversation_id,
        filename="manual.pdf",
        source_kind=SourceKind.PDF,
        content_hash="test-hash",
        status=DocumentStatus.INDEXED,
    )
    await repository.create_document(document)
    first = Chunk(
        id="chunk-first",
        document_id=document.id,
        conversation_id=conversation_id,
        text="The quantized vector index cuts memory usage by four times.",
        source_ref=PageRef(page=1),
        chunk_index=0,
    )
    second = Chunk(
        id="chunk-second",
        document_id=document.id,
        conversation_id=conversation_id,
        text="Scalar quantization stores a min and max per vector.",
        source_ref=PageRef(page=2),
        chunk_index=1,
    )
    await repository.create_chunks([first, second])

    index = NumpyFlatIndex()
    index.add([first.id, second.id], await embedder.embed_documents([first.text, second.text]))

    engine = RagEngine(repository=repository, embedder=embedder, top_k=2, min_similarity=0.0)

    events = [
        event
        async for event in engine.answer(
            conversation_id, "how does quantization save memory", [], index, llm_driver
        )
    ]

    done = events[-1]
    assert isinstance(done, DoneEvent)
    # Internal [chunk_N] tags must never leak into the displayed text.
    assert "chunk_" not in done.message.content
    # Reader-facing [N] markers are stripped too — citations are already
    # rendered as clickable badges in the side panel.
    assert "[1]" not in done.message.content
    assert "[2]" not in done.message.content
    # The citation list on the message object is unaffected — both chunks
    # still appear as structured citations for the side panel.
    assert len(done.message.citations) == 2
    assert {citation.document_id for citation in done.message.citations} == {document.id}


async def test_citation_verification_drops_ids_that_were_never_retrieved(
    repository: Repository, embedder: MockEmbedder
) -> None:
    conversation_id = "conv-4"
    chunk = await _seed_chunk(
        repository, conversation_id, "The drone's max flight time is 28 minutes on a full battery."
    )
    index = NumpyFlatIndex()
    index.add([chunk.id], await embedder.embed_documents([chunk.text]))

    engine = RagEngine(repository=repository, embedder=embedder, top_k=5, min_similarity=0.0)

    events = [
        event
        async for event in engine.answer(
            conversation_id, "flight time", [], index, _FabricatingDriver()
        )
    ]

    done = events[-1]
    assert isinstance(done, DoneEvent)
    # Only chunk_1 was actually retrieved for this turn — chunk_99 must
    # never surface as a citation, no matter what the model's text claims.
    assert len(done.message.citations) == 1
    assert done.message.citations[0].chunk_id == chunk.id
    # The fabricated "[chunk_99]" tag must not leak into the displayed
    # text either — it's stripped entirely, not left as a dangling ref.
    assert "chunk_" not in done.message.content
    assert "99" not in done.message.content
    # The verified citation marker is also stripped from display text
    # (citations live in the side panel, not inline).
    assert "[1]" not in done.message.content


async def test_greeting_bypasses_embedding_and_llm(
    repository: Repository, embedder: MockEmbedder, llm_driver: MockLLMDriver
) -> None:
    """A greeting or farewell must return instantly — no embedding, no
    retrieval, no LLM inference, exactly one DoneEvent."""
    await repository.create_conversation(Conversation(id="conv-greet", title="test"))
    # Indexed content that would match if we actually searched — proves we
    # never get that far.
    document = Document(
        id="doc-greet",
        conversation_id="conv-greet",
        filename="manual.pdf",
        source_kind=SourceKind.PDF,
        content_hash="greet-hash",
        status=DocumentStatus.INDEXED,
    )
    await repository.create_document(document)
    chunk = Chunk(
        id="chunk-greet",
        document_id=document.id,
        conversation_id="conv-greet",
        text="The drone's max flight time is 28 minutes.",
        source_ref=PageRef(page=1),
        chunk_index=0,
    )
    await repository.create_chunks([chunk])
    index = NumpyFlatIndex()
    index.add([chunk.id], await embedder.embed_documents([chunk.text]))

    engine = RagEngine(repository=repository, embedder=embedder, top_k=5, min_similarity=0.0)

    # Greeting: must skip everything and return the canned response.
    greeting_events = [
        event
        async for event in engine.answer(
            "conv-greet", "Hello!", [], index, llm_driver
        )
    ]
    assert len(greeting_events) == 1
    assert isinstance(greeting_events[0], DoneEvent)
    assert greeting_events[0].message.content == _GREETING_RESPONSE
    assert greeting_events[0].message.citations == []

    # Farewell: same property.
    farewell_events = [
        event
        async for event in engine.answer(
            "conv-greet", "Bye!", [], index, llm_driver
        )
    ]
    assert len(farewell_events) == 1
    assert isinstance(farewell_events[0], DoneEvent)
    assert farewell_events[0].message.content == _FAREWELL_RESPONSE
    assert farewell_events[0].message.citations == []

    # A real question with a greeting prefix: must NOT be absorbed.
    real_events = [
        event
        async for event in engine.answer(
            "conv-greet",
            "hi, what's the drone's max flight time?",
            [],
            index,
            llm_driver,
        )
    ]
    tokens = "".join(
        event.text for event in real_events if isinstance(event, TokenEvent)
    )
    assert tokens.strip() != ""
    done = real_events[-1]
    assert isinstance(done, DoneEvent)
    # Must be a real answer, not the canned greeting.
    assert done.message.content != _GREETING_RESPONSE
    assert len(done.message.citations) > 0
