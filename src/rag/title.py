"""Generates a short conversation title from its first exchange — the same
kind of feature Claude/ChatGPT/Gemini all have. Deliberately a separate,
tiny LLM call rather than reusing rag/prompts.py's grounding prompt: no
<context>, no citations, an entirely different concern. That's also what
lets MockLLMDriver (src/llm/mock_driver.py) tell the two apart in tests —
a system prompt with no <context> tags at all is never a RAG answer.
"""

from __future__ import annotations

from src.llm.base import LLMDriver
from src.models import DEFAULT_CONVERSATION_TITLE, LLMMessage

_TITLE_SYSTEM_PROMPT = (
    "Generate a short title for a chat that starts with the user message below. "
    "3 to 6 words, title case, no surrounding quotes, no trailing punctuation. "
    "Reply with the title text only, nothing else."
)

_MAX_TITLE_LENGTH = 80


async def generate_title(question: str, driver: LLMDriver) -> str:
    messages = [
        LLMMessage(role="system", content=_TITLE_SYSTEM_PROMPT),
        LLMMessage(role="user", content=question),
    ]
    raw = "".join([token async for token in driver.generate(messages)])
    # A well-behaved model just replies with the title, but strip a wrapping
    # quote mark defensively — some models quote their answer even when told
    # not to, and a literal quote sitting in a sidebar title looks like a bug.
    title = " ".join(raw.split()).strip(" \"'.")
    return title[:_MAX_TITLE_LENGTH] if title else DEFAULT_CONVERSATION_TITLE
