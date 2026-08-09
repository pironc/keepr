"""Builds the configured LLMDriver from Settings."""

from __future__ import annotations

from src.config import Settings
from src.llm.base import LLMDriver
from src.llm.mock_driver import MockLLMDriver


def build_llm_driver(settings: Settings) -> LLMDriver:
    if settings.llm_driver == "mock":
        return MockLLMDriver()
    if settings.llm_driver == "llama_cpp":
        from src.llm.llama_cpp_driver import LlamaCppDriver

        return LlamaCppDriver(
            model_path=settings.llm_model_path,
            n_ctx=settings.llm_context_window,
            n_gpu_layers=settings.llm_gpu_layers,
        )
    raise ValueError(f"Unknown LLM_DRIVER: {settings.llm_driver!r}")
