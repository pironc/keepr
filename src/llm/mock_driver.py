"""Deterministic mock LLM driver.

No model, no download, no inference. The RAG engine itself owns the
actual grounding decision (a retrieval-confidence threshold gates whether
the LLM is even called at all — see src/rag/engine.py), so this driver's
only job is to plausibly "answer using the system prompt's <context>"
well enough that citation-extraction and streaming can be exercised in
tests without a real model's occasional unpredictability.

Not every call into an LLMDriver is a RAG answer, though — conversation-title
generation (src/rag/title.py) uses the same driver with a completely
different, <context>-free prompt. `_extract_context_block` returning `None`
(no <context> tags at all) vs. `""` (tags present, empty inside) is what
lets this driver tell "not a RAG call" apart from "RAG call, nothing
retrieved" — the latter must still produce the refusal text unchanged.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

from src.llm.base import LLMDriver
from src.models import LLMMessage

_CONTEXT_BLOCK_PATTERN = re.compile(r"<context>\n(.*?)\n</context>", re.DOTALL)
_CONTEXT_ENTRY_PATTERN = re.compile(r"\[(chunk_\d+)\]\s*(.+)")


def _extract_context_block(system_prompt: str) -> str | None:
    # The prompt template itself contains an example citation ("e.g. [chunk_3]")
    # in its instructions — scanning the whole prompt would match that
    # example and echo the instruction text after it as if it were a real
    # retrieved chunk. Only the <context>...</context> block is real data.
    match = _CONTEXT_BLOCK_PATTERN.search(system_prompt)
    return match.group(1) if match else None


class MockLLMDriver(LLMDriver):
    async def generate(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        system_message = next((m for m in messages if m.role == "system"), None)
        context_block = _extract_context_block(system_message.content or "") if system_message else None

        if context_block is None:
            # No <context> tags at all: not a RAG answer, so there's nothing
            # to ground a refusal decision in either. Echo a few words of the
            # user's own message back deterministically — good enough for a
            # title-generation call under test, and for any other future
            # non-RAG use of the same driver.
            user_message = next((m for m in reversed(messages) if m.role == "user"), None)
            response = " ".join((user_message.content if user_message else "").split()[:4]) or "Untitled"
        else:
            entries = _CONTEXT_ENTRY_PATTERN.findall(context_block)
            if not entries:
                response = "I don't have enough information in the retrieved documents to answer this."
            else:
                text_parts = [text.strip()[:80] for _, text in entries]
                tag_parts = [f"[{tag}]" for tag, _ in entries]
                response = " ".join(text_parts)
                if tag_parts:
                    response += "  " + " ".join(tag_parts)

        for word in response.split(" "):
            yield word + " "
