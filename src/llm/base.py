"""Abstract LLM driver: streams generated text given a prompt message list."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from src.models import LLMMessage


class LLMDriver(ABC):
    @abstractmethod
    def generate(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        """Stream the model's response, one token/fragment at a time."""
        ...

    async def availability(self) -> str | None:
        """Return a human-readable reason the language model can't be used, or
        ``None`` if it is usable.  Must be cheap (no model load) so callers can
        probe sibling-model availability without triggering an expensive GGUF
        load.  Defaults to ``None`` (always available) — the mock driver."""
        return None
