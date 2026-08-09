"""Builds the configured Embedder from Settings."""

from __future__ import annotations

from src.config import Settings
from src.embeddings.base import Embedder
from src.embeddings.mock_embedder import MockEmbedder


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedder == "mock":
        return MockEmbedder()
    if settings.embedder == "llama_cpp":
        from src.embeddings.llama_cpp_embedder import LlamaCppEmbedder

        return LlamaCppEmbedder(
            model_path=settings.embedding_model_path, n_gpu_layers=settings.embedding_gpu_layers
        )
    raise ValueError(f"Unknown EMBEDDER: {settings.embedder!r}")
