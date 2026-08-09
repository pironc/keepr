"""Builds/loads the configured VectorIndex backend."""

from __future__ import annotations

from pathlib import Path

from src.vectorstore.base import VectorIndex
from src.vectorstore.flat_index import NumpyFlatIndex
from src.vectorstore.quantized_flat_index import QuantizedNumpyFlatIndex


def new_index(backend: str) -> VectorIndex:
    if backend == "flat":
        return NumpyFlatIndex()
    if backend == "quantized":
        return QuantizedNumpyFlatIndex()
    raise ValueError(f"Unknown VECTOR_INDEX_BACKEND: {backend!r}")


def load_index(backend: str, path: Path) -> VectorIndex:
    if backend == "flat":
        return NumpyFlatIndex.load(path)
    if backend == "quantized":
        return QuantizedNumpyFlatIndex.load(path)
    raise ValueError(f"Unknown VECTOR_INDEX_BACKEND: {backend!r}")
