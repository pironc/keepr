"""Tests for MockLLMDriver — specifically regression-proofing the bug
where its citation-extraction regex could match the example citation
embedded in the prompt template's own instructions ("e.g. [chunk_3]"),
mistaking rule text for a real retrieved chunk.
"""

from __future__ import annotations

from src.llm.mock_driver import MockLLMDriver
from src.models import LLMMessage
from src.rag.prompts import REFUSAL_TEXT, build_context_block, build_system_prompt


async def _generate(system_prompt: str, question: str) -> str:
    driver = MockLLMDriver()
    messages = [
        LLMMessage(role="system", content=system_prompt),
        LLMMessage(role="user", content=question),
    ]
    answer = ""
    async for token in driver.generate(messages):
        answer += token
    return answer


async def test_does_not_match_the_example_citation_in_the_prompt_template() -> None:
    system_prompt = build_system_prompt(build_context_block([("chunk_1", "The sky is blue.")]))

    answer = await _generate(system_prompt, "What color is the sky?")

    assert answer.count("[chunk_") == 1
    assert "[chunk_1]" in answer
    assert "If you" not in answer


async def test_echoes_each_real_context_entry_exactly_once() -> None:
    entries = [("chunk_1", "First fact."), ("chunk_2", "Second fact.")]
    system_prompt = build_system_prompt(build_context_block(entries))

    answer = await _generate(system_prompt, "question")

    assert "[chunk_1]" in answer
    assert "[chunk_2]" in answer
    assert answer.count("[chunk_") == 2


async def test_refuses_when_context_is_empty() -> None:
    system_prompt = build_system_prompt(build_context_block([]))

    answer = await _generate(system_prompt, "question")

    assert answer.strip() == REFUSAL_TEXT


async def test_echoes_the_user_message_when_system_prompt_has_no_context_tags() -> None:
    # A system prompt with <context> tags present but empty (the case above)
    # must still mean "nothing retrieved, refuse" — but a system prompt with
    # no <context> tags at all (e.g. rag/title.py's title-generation prompt)
    # is a completely different kind of call, so it must NOT get the RAG
    # refusal text, which is specifically about missing *retrieved* context.
    answer = await _generate("Some unrelated system instruction.", "What are Basikon's AI policies?")

    assert answer.strip() != REFUSAL_TEXT
    assert answer.strip() == "What are Basikon's AI"
