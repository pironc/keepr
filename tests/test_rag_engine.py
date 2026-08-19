"""Tests for RagEngine: confidence-gated refusal and citation-ID verification.

These are the two properties this project's "anti-hallucination" claim
actually rests on — both are deterministic and asserted here, not just
described in a prompt.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from src.db.repository import Repository
from src.embeddings.mock_embedder import MockEmbedder
from src.llm.base import LLMDriver
from src.llm.mock_driver import MockLLMDriver
from src.model_unavailable import ModelUnavailableError
from src.models import (
    Chunk,
    Conversation,
    Document,
    DocumentStatus,
    LLMMessage,
    Message,
    MessageStatus,
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


class _BareCitationDriver(LLMDriver):
    """A driver that writes bare [N] citations directly instead of the
    required [chunk_N] format — deviating exactly the way a real model
    sometimes does — split across several yields (mid-marker, in one case)
    to exercise _stream_strip_citation_tags' cross-chunk buffering the same
    way a real token-by-token stream would, not just a tag that happens to
    arrive whole in a single token."""

    async def generate(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        for piece in ["The manual says this ", "[", "1", "]", " and also this", " [2] here."]:
            yield piece


class _EmbedderQueryFailure:
    """An embedder whose query embedding raises ModelUnavailableError (the
    duck-typed shape the RagEngine consumes via the Embedder protocol)."""

    dimensions = 4

    async def embed_documents(self, texts):  # type: ignore[no-untyped-def]
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    async def embed_query(self, text):  # type: ignore[no-untyped-def]
        raise ModelUnavailableError("Embedding model file not found: models/nomic.gguf")

    async def availability(self):  # type: ignore[no-untyped-def]
        return "Embedding model file not found: models/nomic.gguf"


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


async def test_bare_citation_markers_are_stripped_from_the_live_stream(
    repository: Repository, embedder: MockEmbedder
) -> None:
    """A model that ignores the [chunk_N] instruction and writes a bare [N]
    directly must never have it appear in a streamed TokenEvent either —
    otherwise a reader watches "[1]" flash by as plain text while the
    answer is generating, then disappear the moment the final
    _strip_citation_tags pass reruns on the complete text and removes what
    it never should have shown live in the first place.
    _stream_strip_citation_tags must strip the bare-[N] case
    (_BARE_NUMBER_PATTERN) too, not just [chunk_N]. _BareCitationDriver
    splits a marker across multiple yields specifically so this also
    proves the cross-chunk buffering, not just the same-chunk case."""
    conversation_id = "conv-bare-citation"
    chunk = await _seed_chunk(repository, conversation_id, "Some retrievable manual text.")
    index = NumpyFlatIndex()
    index.add([chunk.id], await embedder.embed_documents([chunk.text]))

    engine = RagEngine(repository=repository, embedder=embedder, top_k=1, min_similarity=0.0)

    events = [
        event
        async for event in engine.answer(
            conversation_id, "manual text", [], index, _BareCitationDriver()
        )
    ]

    streamed_text = "".join(event.text for event in events if isinstance(event, TokenEvent))
    assert "[1]" not in streamed_text
    assert "[2]" not in streamed_text
    # The words around the stripped markers still made it through untouched.
    assert "manual says this" in streamed_text
    assert "also this" in streamed_text
    assert "here" in streamed_text

    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert "[1]" not in done.message.content
    assert "[2]" not in done.message.content


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


class _DoneDriver(LLMDriver):
    """A driver that would generate if reached — proves the query-embed failure
    short-circuits before inference."""

    async def generate(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        yield "I should never be reached."


async def test_missing_embedder_query_failure_is_a_clean_error_not_a_crash(
    repository: Repository,
) -> None:
    """When the embedding model is missing, query embedding raises
    ModelUnavailableError. With a message_id (GenerationWorker path) the engine
    must finalize an ERROR message and yield a DoneEvent rather than crash —
    so a text-only question never produces a generic 'Something went wrong'."""
    await repository.create_conversation(Conversation(id="conv-qfail", title="test"))

    engine = RagEngine(repository=repository, embedder=_EmbedderQueryFailure(), top_k=5, min_similarity=0.0)
    index = NumpyFlatIndex()
    driver = _DoneDriver()
    # Mirrors how GenerationWorker drives the engine: a QUEUED assistant
    # placeholder row exists before answer() is called, and finalize_message()
    # UPDATEs that placeholder on the error path.
    message_id = "msg-qfail"
    await repository.create_message(
        Message(
            id=message_id,
            conversation_id="conv-qfail",
            role="assistant",
            content="",
            status=MessageStatus.QUEUED,
        )
    )

    events = [
        event
        async for event in engine.answer(
            "conv-qfail", "what's the drone range?", [], index, driver, message_id=message_id
        )
    ]

    # The final event is a DoneEvent, proof the engine returned rather than
    # raising; its message is marked ERROR and carries the actionable text.
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].message.status == MessageStatus.ERROR
    assert events[-1].message.error_message is not None
    assert "Embedding model file not found" in events[-1].message.error_message

    # The persisted placeholder reflects the ERROR.
    placeholder = await repository.get_message(message_id)
    assert placeholder is not None
    assert placeholder.status == MessageStatus.ERROR
    assert placeholder.error_message is not None
    assert "Embedding model file not found" in placeholder.error_message

    # Without a message_id (direct-call tests), today's behavior — propagate.
    with pytest.raises(ModelUnavailableError):
        events = [
            event
            async for event in engine.answer(
                "conv-qfail", "what's the drone range?", [], index, driver
            )
        ]


class _EmbedderOnlyMissingDriver(LLMDriver):
    """An LLM driver that IS available (retrieval failed before it was ever
    called) — proves the "only embedding model missing" message fires when the
    language model companion is fine."""

    async def generate(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        yield "I should never be reached."


class _BothMissingDriver(LLMDriver):
    """An LLM driver whose sibling-availability probe reports a missing language
    model file — proves the "both models unavailable" message fires when the
    user has neither model installed, and that it does so WITHOUT ever reaching
    generation (the engine short-circuits on both-missing)."""

    def __init__(self) -> None:
        self.generate_calls = 0

    async def generate(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        self.generate_calls += 1
        yield "I should never be reached."

    async def availability(self) -> str | None:
        return "Language model file not found: models/qwen.gguf"


class _LanguageModelMissingDriver(LLMDriver):
    """An LLM driver that raises ModelUnavailableError at generate time — the
    language model file exists-and-was-selected but fails to load, and
    retrieval (which uses the embedder) already succeeded.  The unreachable
    ``yield`` keeps this an async-generator function (matching real drivers)
    so ``async for`` in the engine sees the exception on first iteration."""

    async def generate(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        def _raise() -> None:
            raise ModelUnavailableError(
                "Language model could not be loaded — the file may be corrupted "
                "or the wrong architecture for this build of keepr: models/qwen.gguf."
            )

        _raise()
        yield "unreachable"  # keeps this an async generator; never produced


async def _placeholder_message(
    repository: Repository, conversation_id: str, message_id: str
) -> None:
    """Insert the QUEUED assistant placeholder GenerationWorker would have
    created, so the engine's ERROR path finalizes (UPDATEs) it."""
    await repository.create_message(
        Message(
            id=message_id,
            conversation_id=conversation_id,
            role="assistant",
            content="",
            status=MessageStatus.QUEUED,
        )
    )


async def test_embedder_and_llm_both_missing_message_acknowledges_both(
    repository: Repository,
) -> None:
    """With neither an embedding nor a language model, the error names BOTH,
    not just the embedder that happens to fail first."""
    conversation_id = "conv-both"
    await repository.create_conversation(Conversation(id=conversation_id, title="test"))
    engine = RagEngine(
        repository=repository, embedder=_EmbedderQueryFailure(), top_k=5, min_similarity=0.0
    )
    message_id = "msg-both"
    await _placeholder_message(repository, conversation_id, message_id)
    driver = _BothMissingDriver()
    events = [
        event
        async for event in engine.answer(
            conversation_id, "what's the drone range?", [], NumpyFlatIndex(),
            driver, message_id=message_id,
        )
    ]

    # The short-circuit must fire BEFORE retrieval/generation: the driver's
    # generate() is never reached, so the reply is instant (no thinking).
    assert driver.generate_calls == 0

    assert isinstance(events[-1], DoneEvent)
    assert events[-1].message.status == MessageStatus.ERROR
    text = events[-1].message.content
    # Concise: just names that neither model is downloaded.
    assert "embedding model" in text and "language model" in text
    assert "is downloaded" in text


async def test_both_missing_direct_call_short_circuits_without_raising(
    repository: Repository,
) -> None:
    """Even on the direct-call path (message_id None), a both-missing state
    now returns the canned reply instead of attempting retrieval and raising.
    This is the instant check: the driver is never asked to generate, so there
    is no 'thinking' before the answer appears."""
    conversation_id = "conv-both-direct"
    await repository.create_conversation(Conversation(id=conversation_id, title="test"))
    engine = RagEngine(
        repository=repository, embedder=_EmbedderQueryFailure(), top_k=5, min_similarity=0.0
    )
    driver = _BothMissingDriver()
    events = [
        event
        async for event in engine.answer(
            conversation_id, "what's the drone range?", [], NumpyFlatIndex(), driver
        )
    ]

    assert driver.generate_calls == 0
    assert isinstance(events[-1], DoneEvent)
    # On the direct-call path the canned reply is returned as a normal message
    # (direct-call _finalize creates a fresh DONE message — the ERROR status we
    # set only lands on the GenerationWorker placeholder via message_id).  The
    # point here is content + no generate() call, so the reply is instant.
    assert "neither an embedding model nor a language model is downloaded" in events[-1].message.content


async def test_greeting_does_not_bypass_missing_model_check(
    repository: Repository,
) -> None:
    """A bare greeting with no models installed must surface the "no model"
    error, not the canned greeting — the availability check runs first, so
    "hi" can't masquerade as a healthy session when nothing is downloaded."""
    conversation_id = "conv-greet-nomodel"
    await repository.create_conversation(Conversation(id=conversation_id, title="test"))
    engine = RagEngine(
        repository=repository, embedder=_EmbedderQueryFailure(), top_k=5, min_similarity=0.0
    )
    driver = _BothMissingDriver()
    events = [
        event
        async for event in engine.answer(
            conversation_id, "Hello!", [], NumpyFlatIndex(), driver
        )
    ]

    assert driver.generate_calls == 0
    assert isinstance(events[-1], DoneEvent)
    # The both-missing reply, not the greeting canned text.
    assert events[-1].message.content != _GREETING_RESPONSE
    assert "neither an embedding model nor a language model is downloaded" in events[-1].message.content


async def test_only_embedding_model_missing_message_is_scoped(
    repository: Repository,
) -> None:
    """When the LLM is fine, the error is scoped to the embedding model only —
    it must not claim the language model is unavailable."""
    conversation_id = "conv-embonly"
    await repository.create_conversation(Conversation(id=conversation_id, title="test"))
    engine = RagEngine(
        repository=repository, embedder=_EmbedderQueryFailure(), top_k=5, min_similarity=0.0
    )
    message_id = "msg-embonly"
    await _placeholder_message(repository, conversation_id, message_id)
    events = [
        event
        async for event in engine.answer(
            conversation_id, "what's the drone range?", [], NumpyFlatIndex(),
            _EmbedderOnlyMissingDriver(), message_id=message_id,
        )
    ]

    assert isinstance(events[-1], DoneEvent)
    assert events[-1].message.status == MessageStatus.ERROR
    text = events[-1].message.content
    assert "embedding model" in text
    assert "no embedding model is installed" in text
    assert "language model" not in text  # the LLM is fine — don't blame it
    # The per-file reason is preserved for diagnostics, not in the chat text.
    assert "Embedding model file not found" in (events[-1].message.error_message or "")


async def test_only_language_model_missing_message_is_scoped(
    repository: Repository, embedder: MockEmbedder
) -> None:
    """Retrieval succeeds but the language model fails to load: the error is
    scoped to the language model and surfaces the load/corruption reason."""
    conversation_id = "conv-llmonly"
    chunk = await _seed_chunk(
        repository,
        conversation_id,
        "The drone's max flight time is 28 minutes on a full battery.",
    )
    index = NumpyFlatIndex()
    index.add([chunk.id], await embedder.embed_documents([chunk.text]))

    engine = RagEngine(repository=repository, embedder=embedder, top_k=5, min_similarity=0.0)
    driver = _LanguageModelMissingDriver()
    message_id = "msg-llmonly"
    await _placeholder_message(repository, conversation_id, message_id)
    events = [
        event
        async for event in engine.answer(
            conversation_id, "drone flight time", [], index, driver, message_id=message_id
        )
    ]

    assert isinstance(events[-1], DoneEvent)
    assert events[-1].message.status == MessageStatus.ERROR
    text = events[-1].message.content
    assert "language model" in text
    assert "no language model is installed" in text
    assert "embedding model" not in text  # retrieval worked — don't blame the embedder
    # The load/corruption reason is preserved for diagnostics, not in the chat text.
    assert "could not be loaded" in (events[-1].message.error_message or "")
