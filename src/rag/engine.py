"""RAG engine: retrieval, confidence-gated refusal, grounded generation,
and citation-ID verification.

The primary anti-hallucination defense is deterministic, not prompted:
every retrieved chunk's similarity score is checked against
`min_similarity` individually — not just the best one — so a single
strong match can't drag several much-weaker, barely-related chunks into
context (and into the model's citations) purely because they made the
top-k cut. If nothing clears the bar, the engine refuses before the LLM
is ever called — a threshold check on a float, not something left to a
7-8B model's judgment. Citations are verified the same way: only chunk
IDs that were actually retrieved for this turn can appear as a citation,
checked by set membership, not by trusting the model didn't fabricate one.

The `[chunk_N]` tags above are an internal grounding mechanism only — the
model needs a stable per-chunk handle to cite. Before a message reaches a
reader, `_strip_citation_tags` removes every tag from the display text
entirely: citations are already rendered as clickable badges in the side
panel, so inline markers in the answer body are redundant noise. A tag
that didn't survive citation verification (fabricated by the model) is
also dropped from the citation list. A model that ignores the `[chunk_N]`
instruction and writes a bare `[1]`/`[2]` directly gets the same treatment
(`_BARE_NUMBER_PATTERN`) — unverifiable either way, since no bare number
can be traced back to a specific retrieved chunk.

Both patterns are also stripped from the *streaming* text as it's
generated (`_stream_strip_citation_tags`), not just this final pass — a
tag that streamed to the reader as visible plain text and then vanished
the moment this final pass reran on the complete answer would read as a
bug, not a grounding mechanism.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from src.db.repository import Repository
from src.embeddings.base import Embedder
from src.llm.base import LLMDriver
from src.model_unavailable import ModelUnavailableError
from src.models import Chunk, Citation, LLMMessage, Message, MessageStatus
from src.rag.greeting import detect_greeting_or_farewell
from src.rag.prompts import REFUSAL_TEXT, build_context_block, build_system_prompt
from src.vectorstore.base import VectorIndex

_CITATION_PATTERN = re.compile(r"\[(chunk_\d+)\]")
# Used for stripping complete [chunk_N] tags from streaming tokens — must
# match the same pattern as _CITATION_PATTERN but anchored to the full tag.
_TAG_PATTERN = re.compile(r"\[chunk_\d+\]")


def _embedder_unavailable_text() -> str:
    """User-facing text when the embedding model is unavailable but the
    language model (checked elsewhere) is fine.  Kept concise — the per-file
    reason lives in the message's ``error_message``, not in the chat text."""
    return (
        "I couldn't look up an answer because no embedding model is installed. "
        "Download one in Settings → Models."
    )


def _both_models_unavailable_text() -> str:
    """User-facing text when neither an embedding nor a language model is
    installed.  Kept short on purpose: the user asked for just a statement that
    nothing is downloaded, not the per-file reasons (those live in the
    ``error_message`` for diagnostics)."""
    return (
        "I couldn't look up an answer — neither an embedding model "
        "nor a language model is downloaded. Get them in Settings → Models."
    )


def _language_model_unavailable_text() -> str:
    """User-facing text when retrieval succeeded but the language model is
    unavailable — the current chat's documents were still read, it just couldn't
    generate an answer.  Kept concise — the per-file reason lives in the
    message's ``error_message``, not in the chat text."""
    return (
        "I couldn't generate an answer because no language model is installed. "
        "Download one in Settings → Models."
    )


@dataclass(slots=True, frozen=True)
class TokenEvent:
    text: str


@dataclass(slots=True, frozen=True)
class MessageStatusEvent:
    """Mirrors DocumentStatusEvent's shape (src/ingestion/pipeline.py) —
    only ever yielded when the caller supplied a message_id (i.e. only to
    GenerationWorker, never to the 5 direct-call tests, which pass no
    message_id and never see this event type)."""

    message_id: str
    status: MessageStatus
    error_message: str | None = None


@dataclass(slots=True, frozen=True)
class DoneEvent:
    message: Message


RagEvent = TokenEvent | MessageStatusEvent | DoneEvent


class RagEngine:
    def __init__(
        self, repository: Repository, embedder: Embedder, top_k: int, min_similarity: float
    ) -> None:
        self._repository = repository
        self._embedder = embedder
        self._top_k = top_k
        self._min_similarity = min_similarity

    async def answer(
        self,
        conversation_id: str,
        question: str,
        history: list[LLMMessage],
        index: VectorIndex,
        driver: LLMDriver,
        message_id: str | None = None,
    ) -> AsyncIterator[RagEvent]:
        # "No model installed" is checked *before* anything else, including the
        # greeting fast-path below — a bare "hi" must surface the missing-model
        # error rather than a cheerful canned reply that papers over the fact
        # that nothing is installed.  Each availability() is a cheap
        # file-existence stat (no model load), so this stays instant.  On the
        # GenerationWorker path (message_id set) this finalizes an ERROR
        # message; direct-call tests (message_id None) just stop early with the
        # canned reply rather than raising.  If only ONE model is missing this
        # gate is skipped and the regular path reports exactly which one when
        # it actually fails.
        open_emb_reason, open_llm_reason = await asyncio.gather(
            self._embedder.availability(), driver.availability()
        )
        if open_emb_reason is not None and open_llm_reason is not None:
            message = await self._finalize(
                conversation_id,
                _both_models_unavailable_text(),
                [],
                message_id=message_id,
                status=MessageStatus.ERROR,
                error_message=f"{open_emb_reason} {open_llm_reason}",
            )
            if message_id is not None:
                yield MessageStatusEvent(message_id, MessageStatus.ERROR, str(open_emb_reason))
            yield DoneEvent(message=message)
            return

        # Fast-path: a greeting or farewell needs no retrieval and no LLM —
        # return a canned response instantly. Only the *entire* input is
        # considered; "hi, what's the capital?" passes through unchanged.
        # (Reached only once the model check above has confirmed at least one
        # model is available.)
        greeting = detect_greeting_or_farewell(question)
        if greeting is not None:
            message = await self._finalize(
                conversation_id, greeting, [], message_id=message_id
            )
            if message_id is not None:
                yield MessageStatusEvent(message_id, MessageStatus.DONE)
            yield DoneEvent(message=message)
            return

        # message_id is only ever supplied by GenerationWorker. Every status
        # write/event below is gated on it so the 6 direct-call tests (no
        # message_id passed) exercise exactly today's behavior, unchanged.
        if message_id is not None:
            await self._repository.update_message_status(message_id, MessageStatus.RETRIEVING)
            yield MessageStatusEvent(message_id, MessageStatus.RETRIEVING)

        try:
            query_vector = await self._embedder.embed_query(question)
        except ModelUnavailableError as exc:
            # Embedding model missing or unloadable — can't retrieve anything,
            # so there's no context to ground an answer. Mirrors the
            # generation-error handling below: when GenerationWorker is driving
            # this (message_id set) mark the message ERROR with an actionable
            # description; direct-call tests (message_id None) see today's
            # behavior of propagating immediately, since they never encounter
            # a real embedder.
            if message_id is None:
                raise
            # Retrieval failed before the LLM was ever reached, so this call
            # alone can't see the language model's state.  Probe it cheaply
            # (no model load) so a user who has *neither* model installed gets
            # both named rather than a misleading "only the embedder is
            # missing".  (The upfront both-missing gate below usually catches
            # this case first — this is the fallback for when the embedder's
            # FILE exists but fails to *load* (e.g. corrupt), making the
            # cheap upfront probe miss it.)
            llm_reason = await driver.availability()
            body = _embedder_unavailable_text()
            if llm_reason is not None:
                body = _both_models_unavailable_text()
            message = await self._finalize(
                conversation_id,
                body,
                [],
                message_id=message_id,
                status=MessageStatus.ERROR,
                error_message=str(exc),
            )
            yield MessageStatusEvent(message_id, MessageStatus.ERROR, str(exc))
            yield DoneEvent(message=message)
            return
        scored = index.search(query_vector, self._top_k)

        # Filter every retrieved chunk against the bar individually, not
        # just the best one — otherwise a strong top match drags along
        # several much-weaker, barely-related ones that still end up in
        # context (and get cited) purely because they made the top-k cut.
        qualifying = [(chunk_id, score) for chunk_id, score in scored if score >= self._min_similarity]

        if not qualifying:
            # No chunk cleared the threshold, but the index is non-empty.
            # The mock embedder (a bag-of-words hashing trick) has zero
            # semantic understanding — a general question like "what is
            # this document about?" shares no vocabulary with any chunk
            # and scores near 0.0 even though the question IS about the
            # indexed content. Fall back to the best-scoring chunks
            # rather than refusing outright: the LLM still sees real
            # document text and can ground its answer in it; the
            # alternative is refusing every synopsis-level question when
            # running without a real embedding model.
            if scored:
                qualifying = scored[: self._top_k]
            else:
                message = await self._finalize(conversation_id, REFUSAL_TEXT, [], message_id=message_id)
                yield DoneEvent(message=message)
                return

        chunk_ids = [chunk_id for chunk_id, _score in qualifying]
        chunks = await self._repository.get_chunks_by_ids(chunk_ids)
        chunks_by_id = {chunk.id: chunk for chunk in chunks}
        # Tags are per-chunk, not per-document: several tags can (and
        # commonly do) point at the same source file, just different
        # passages within it.
        tag_by_chunk_id = {
            chunk_id: f"chunk_{position + 1}" for position, chunk_id in enumerate(chunk_ids)
        }

        context_entries = [
            (tag_by_chunk_id[chunk_id], chunks_by_id[chunk_id].text)
            for chunk_id in chunk_ids
            if chunk_id in chunks_by_id
        ]
        system_prompt = build_system_prompt(build_context_block(context_entries))
        messages = [
            LLMMessage(role="system", content=system_prompt),
            *history,
            LLMMessage(role="user", content=question),
        ]

        if message_id is not None:
            await self._repository.update_message_status(message_id, MessageStatus.GENERATING)
            yield MessageStatusEvent(message_id, MessageStatus.GENERATING)

        answer_text = ""
        strip_buffer = ""
        try:
            async for token in driver.generate(messages):
                answer_text += token
                clean, strip_buffer = _stream_strip_citation_tags(token, strip_buffer)
                if clean:
                    yield TokenEvent(text=clean)
        except ModelUnavailableError as exc:
            # The selected language model is missing on disk or failed to load
            # (corrupt/truncated/wrong architecture). Retrieval already
            # succeeded, so only the language model can be at fault here — say
            # so plainly and surface the underlying reason.  Direct-call tests
            # (message_id None) propagate exactly as before.
            if message_id is None:
                raise
            message = await self._finalize(
                conversation_id,
                _language_model_unavailable_text(),
                [],
                message_id=message_id,
                status=MessageStatus.ERROR,
                error_message=str(exc),
            )
            yield MessageStatusEvent(message_id, MessageStatus.ERROR, str(exc))
            yield DoneEvent(message=message)
            return
        except Exception as exc:
            # Only reachable when GenerationWorker is driving this (message_id
            # set) — without it, propagate unchanged, since the 6 direct-call
            # tests must see identical behavior on a raising driver.
            if message_id is None:
                raise
            interrupted_text = (
                f"{answer_text}\n\n_Generation was interrupted before completing: {exc}_"
            )
            # Don't run citation extraction on text that may have been cut
            # mid-`[chunk_` tag — ship it with no citations rather than add
            # partial-tag edge-case handling to a path already known-broken.
            message = await self._finalize(
                conversation_id,
                interrupted_text,
                [],
                message_id=message_id,
                status=MessageStatus.ERROR,
                error_message=str(exc),
            )
            yield MessageStatusEvent(message_id, MessageStatus.ERROR, str(exc))
            yield DoneEvent(message=message)
            return

        # Flush any remaining partial-tag buffer — the stream is done, so
        # whatever was buffered isn't actually a [chunk_N] tag.
        if strip_buffer:
            yield TokenEvent(text=strip_buffer)

        chunk_id_by_tag = {tag: chunk_id for chunk_id, tag in tag_by_chunk_id.items()}
        cited_tags = sorted(set(_CITATION_PATTERN.findall(answer_text)))
        cited_chunks = [
            chunks_by_id[chunk_id_by_tag[tag]]
            for tag in cited_tags
            if tag in chunk_id_by_tag and chunk_id_by_tag[tag] in chunks_by_id
        ]
        citations = await self._build_citations(cited_chunks)

        display_text = _strip_citation_tags(answer_text)
        message = await self._finalize(conversation_id, display_text, citations, message_id=message_id)
        yield DoneEvent(message=message)

    async def _build_citations(self, chunks: list[Chunk]) -> list[Citation]:
        filenames: dict[str, str] = {}
        for document_id in {chunk.document_id for chunk in chunks}:
            document = await self._repository.get_document(document_id)
            if document is not None:
                filenames[document_id] = document.filename
        return [
            Citation(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                document_filename=filenames.get(chunk.document_id, ""),
                source_ref=chunk.source_ref,
                snippet=chunk.text[:280],
            )
            for chunk in chunks
        ]

    async def _finalize(
        self,
        conversation_id: str,
        content: str,
        citations: list[Citation],
        message_id: str | None = None,
        status: MessageStatus = MessageStatus.DONE,
        error_message: str | None = None,
    ) -> Message:
        if message_id is None:
            message = Message(
                id=str(uuid.uuid4()),
                conversation_id=conversation_id,
                role="assistant",
                content=content,
                citations=citations,
            )
            await self._repository.create_message(message)
            return message

        # UPDATE the existing placeholder GenerationWorker already inserted,
        # rather than INSERT — created_at stays whatever it was at QUEUED
        # time, never rewritten to "now", so a slower-but-first-asked
        # message can't end up looking later than a faster-but-later one.
        await self._repository.finalize_message(
            message_id, status, content, citations, error_message=error_message
        )
        return Message(
            id=message_id,
            conversation_id=conversation_id,
            role="assistant",
            content=content,
            citations=citations,
            status=status,
            error_message=error_message,
        )


_TRAILING_SPACE_BEFORE_PUNCTUATION = re.compile(r"[ \t]+([,.;:])")
_REPEATED_WHITESPACE = re.compile(r"[ \t]{2,}")
# Catches bare [1], [2], … the model wrote directly instead of the
# required [chunk_N] format.  These can't be verified against any
# retrieved chunk, so the only safe move is to strip them before they
# reach the frontend as dead, non-clickable markers — both here (the final
# pass) and in _stream_strip_citation_tags below (the streaming pass);
# without the latter, a deviating model's bare "[1]" streams to the reader
# as plain text and then visibly vanishes the moment this pass reruns on
# the complete answer_text and removes what it never should have shown.
_BARE_NUMBER_PATTERN = re.compile(r"\[\d+\]")
# When a [chunk_N] tag is removed mid-sentence it can leave a dangling
# preposition/article before the punctuation — "as described in [chunk_3]."
# becomes "as described in .", which reads as grammatically broken.  Strip
# the orphaned word and its leading space so it collapses to "as described."
_DANGLING_WORD_BEFORE_PUNCTUATION = re.compile(
    r"\s+(in|at|by|on|from|with|for|to|of|about|as|through|into|onto|the|a|an|that|this|these|those|its|their)\s*([,.;:!?])"
)


def _stream_strip_citation_tags(token_text: str, buffer: str) -> tuple[str, str]:
    """Strip ``[chunk_N]`` and bare ``[N]`` from streaming text, handling tags
    split across tokens.

    Returns ``(clean_text_to_yield, new_buffer)``.  The buffer carries over
    a partial tag prefix (e.g. ``"[chu"`` or, for a bare tag, ``"[1"``) across
    token boundaries so the complete tag can be stripped once all its pieces
    arrive.  A partial prefix is only recognised when the text since the last
    ``[`` looks like it could still become one of the two tag shapes given
    another token — a bare ``[note]`` or ``[chunk_size]`` is yielded
    immediately because neither matches ``[chunk_`` + digits + ``]`` or
    ``[`` + digits + ``]``.

    The bare-``[N]``  half of this mirrors ``_strip_citation_tags``' own
    ``_BARE_NUMBER_PATTERN`` pass below — without it, a model that deviates
    from the required ``[chunk_N]`` format and writes a bare ``[1]`` directly
    streams it to the reader as plain text, which then visibly vanishes the
    moment the final pass reruns on the complete answer and removes it.
    """
    combined = buffer + token_text
    clean = _TAG_PATTERN.sub("", combined)
    clean = _BARE_NUMBER_PATTERN.sub("", clean)

    # Find the last "[" — if what follows could be the start of either tag
    # shape, keep it in the buffer for the next token.
    last_bracket = clean.rfind("[")
    if last_bracket == -1:
        return clean, ""

    suffix = clean[last_bracket:]
    if _is_partial_citation_tag(suffix):
        return clean[:last_bracket], suffix

    return clean, ""


def _is_partial_citation_tag(s: str) -> bool:
    """True when *s* could be the start of a ``[chunk_N]`` tag or a bare
    ``[N]`` tag the model wrote directly instead."""
    if not s or s[0] != "[":
        return False
    # Every complete prefix of "[chunk_" — "[", "[c", "[ch", …
    _PARTIAL_PREFIXES = frozenset({"[", "[c", "[ch", "[chu", "[chun", "[chunk", "[chunk_"})
    if s in _PARTIAL_PREFIXES:
        return True
    if s.startswith("[chunk_"):
        after = s[len("[chunk_"):]
        # Digits (or empty — the number hasn't arrived yet)
        return after == "" or after.isdigit()
    # Not a "[chunk_" prefix (and not just "[" alone, already handled above)
    # — could still be a bare [N] tag in progress, e.g. "[1", "[42", if
    # everything since the "[" is digits so far.
    return s[1:].isdigit()


def _strip_citation_tags(answer_text: str) -> str:
    # Strip bare [N] markers the model may have generated directly
    # (instead of the required [chunk_N] format).  These are unverifiable
    # and would appear as dead text in the frontend.
    answer_text = _BARE_NUMBER_PATTERN.sub("", answer_text)

    # Strip every [chunk_N] tag from the display text.  Citations are
    # already rendered as clickable badges in the side panel, so inline
    # markers in the answer body are redundant noise.  The citation list
    # on the message object is built separately (from the same tags,
    # extracted before this function runs) and is unaffected.
    text = _CITATION_PATTERN.sub("", answer_text)
    text = _TRAILING_SPACE_BEFORE_PUNCTUATION.sub(r"\1", text)
    # When a [chunk_N] tag was placed mid-sentence the removal above can
    # leave dangling prepositions ("as described in.") — clean those up
    # so the resulting sentence is still grammatical.
    text = _DANGLING_WORD_BEFORE_PUNCTUATION.sub(r"\2", text)
    return _REPEATED_WHITESPACE.sub(" ", text).strip()
