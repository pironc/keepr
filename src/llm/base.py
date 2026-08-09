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
