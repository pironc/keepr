"""Unit tests for greet.py — the normalized-pattern matcher that catches
greetings and farewells before any expensive work happens."""

from __future__ import annotations

import pytest

from src.rag.greeting import detect_greeting_or_farewell


@pytest.mark.parametrize(
    "greeting",
    [
        "hi",
        "Hi",
        "HI",
        "Hi!",
        "  hi  ",
        "hi!",
        "hey",
        "hey!",
        "heya",
        "hello",
        "Hello",
        "Hello!",
        "howdy",
        "hiya",
        "hiyah",
        "yo",
        "sup",
        "good morning",
        "good afternoon",
        "good evening",
        "good day",
        "Good morning!",
        "g'day",
        "greetings",
        "how are you",
        "How are you?",
        "how are u",
        "how r u",
        "how r ya",
        "how's it going",
        "how is it going",
        "how are things",
        "how are things going",
        "how do you do",
        "how ya doin",
        "what's up",
        "what is up",
        "what's good",
        "what's new",
        "what's happening",
        "nice to meet you",
        "pleased to meet you",
        "good to see you",
    ],
)
def test_detects_common_greetings(greeting: str) -> None:
    result = detect_greeting_or_farewell(greeting)
    assert result is not None, f"failed to detect: {greeting!r}"
    assert result == "Hello! How can I help you?"


@pytest.mark.parametrize(
    "farewell",
    [
        "bye",
        "Bye",
        "Bye!",
        "goodbye",
        "good bye",
        "Goodbye!",
        "see ya",
        "see you",
        "see you later",
        "see u",
        "later",
        "catch you later",
        "cya",
        "ttyl",
        "thanks",
        "Thanks!",
        "thank you",
        "thank you very much",
        "many thanks",
        "thx",
        "cheers",
        "take care",
        "have a good one",
        "have a good day",
        "have a good night",
        "good night",
        "goodnight",
        "peace",
        "peace out",
        "farewell",
        "I'm off",
        "im off",
        "I am heading out",
        "gotta go",
        "gotta run",
        "talk later",
        "talk to you later",
        "talk soon",
        "  Bye!  ",
    ],
)
def test_detects_common_farewells(farewell: str) -> None:
    result = detect_greeting_or_farewell(farewell)
    assert result is not None, f"failed to detect: {farewell!r}"
    assert result == "Goodbye! Feel free to come back anytime."


@pytest.mark.parametrize(
    "question",
    [
        "hi, what's the capital of France?",
        "hello, tell me about the drone",
        "hey, can you summarize this document?",
        "thanks, but what does the report say about revenue?",
        "bye, one last question — what time is the meeting?",
        "How are you, and also what's the weather?",
        "good morning! do you have the Q3 numbers?",
    ],
)
def test_embedded_greetings_pass_through(question: str) -> None:
    """A greeting that's part of a real question must not be absorbed."""
    assert detect_greeting_or_farewell(question) is None


@pytest.mark.parametrize(
    "non_greeting",
    [
        "What is the drone's max flight time?",
        "tell me about the quarterly results",
        "summarize this document",
        "",
        "   ",
        "!",
        "?",
    ],
)
def test_non_greetings_pass_through(non_greeting: str) -> None:
    assert detect_greeting_or_farewell(non_greeting) is None


def test_punctuation_handling() -> None:
    """Different punctuation flavors on the same base greeting."""
    for variant in ["Hi", "Hi!", "Hi...", "Hi…", "  Hi  ", "hi"]:
        assert detect_greeting_or_farewell(variant) is not None, f"failed: {variant!r}"
