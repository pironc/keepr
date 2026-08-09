"""_strip_thinking is pure string-stream filtering with no dependency on a
real model, so it's tested directly here rather than through LlamaCppDriver
(which needs a real GGUF file to construct at all).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from src.llm.llama_cpp_driver import _strip_thinking


async def _aiter(chunks: list[str]) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


async def _collect(chunks: list[str]) -> str:
    return "".join([token async for token in _strip_thinking(_aiter(chunks))])


async def test_strips_a_complete_think_block() -> None:
    result = await _collect(["<think>\nreasoning here\n</think>\n\nThe answer."])
    assert result == "The answer."


async def test_open_and_close_tags_split_across_many_small_chunks() -> None:
    # Simulates a tokenizer splitting "<think>" and "</think>" mid-tag,
    # which is the exact scenario _strip_thinking's buffering exists for.
    chunks = [*"<think>reasoning</think>", "The", " ", "answer", "."]
    result = await _collect(chunks)
    assert result == "The answer."


async def test_passthrough_when_response_never_opens_a_think_block() -> None:
    result = await _collect(["No ", "thinking ", "here."])
    assert result == "No thinking here."


async def test_content_starting_with_angle_bracket_but_not_think_is_preserved() -> None:
    result = await _collect(["<3 ", "thanks!"])
    assert result == "<3 thanks!"


async def test_leading_whitespace_before_think_block_is_handled() -> None:
    result = await _collect(["\n\n<think>stuff</think>", "Answer."])
    assert result == "Answer."
