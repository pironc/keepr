"""Semantic error for "a configured model file can't be used".

Raised by the llama.cpp LLM driver and embedder when their GGUF file is
missing on disk or fails to load (corrupted / truncated / wrong
architecture / unloadable at that moment). Both the ingestion pipeline and
the RAG engine catch *this* type specifically, so a broken model becomes a
clean, actionable document/message error instead of an escaping llama-cpp
``ValueError`` that crashes the SSE stream with a generic "Something went
wrong".

The ``role`` field distinguishes which model the failure is about, so a
caller that catches the error mid-pipeline (e.g. the RAG engine, where
retrieval fails before the LLM is ever reached) can still phrase the
user-facing message correctly and — combined with the sibling
``availability()`` probe — tell "embedding missing" / "language missing" /
"both missing" apart.
"""

from __future__ import annotations

from enum import StrEnum


class ModelRole(StrEnum):
    EMBEDDING = "embedding"
    LANGUAGE = "language"


class ModelUnavailableError(RuntimeError):
    """Marked as "unavailable" rather than a generic failure so callers can
    tell "your model file is missing or broken" apart from every other
    embed/generate failure and phrase the user-facing message accordingly."""

    def __init__(self, message: str | None = None, *, role: ModelRole = ModelRole.LANGUAGE) -> None:
        super().__init__(message if message is not None else "")
        self.role = role
