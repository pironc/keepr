"""Tests for the hand-rolled vector indexes, including a real quantization
benchmark (memory footprint + retrieval agreement) — not just "it runs,"
actual measured numbers, the same ethos as the LLM-quantization story."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from src.vectorstore.flat_index import NumpyFlatIndex
from src.vectorstore.quantized_flat_index import QuantizedNumpyFlatIndex


def _random_vectors(count: int, dims: int, seed: int) -> NDArray[np.float32]:
    rng = np.random.default_rng(seed)
    return rng.normal(size=(count, dims)).astype(np.float32)


def test_flat_index_returns_exact_nearest_neighbor() -> None:
    index = NumpyFlatIndex()
    vectors = _random_vectors(50, 16, seed=1)
    ids = [f"chunk_{i}" for i in range(50)]
    index.add(ids, vectors)

    results = index.search(vectors[7].copy(), top_k=1)

    assert results[0][0] == "chunk_7"
    assert results[0][1] > 0.99  # cosine similarity to itself is ~1.0


def test_flat_index_save_and_load_round_trip(tmp_path: Path) -> None:
    index = NumpyFlatIndex()
    vectors = _random_vectors(10, 8, seed=2)
    ids = [f"chunk_{i}" for i in range(10)]
    index.add(ids, vectors)

    path = tmp_path / "index.npz"
    index.save(path)
    loaded = NumpyFlatIndex.load(path)

    assert len(loaded) == len(index)
    assert loaded.search(vectors[3], top_k=1)[0][0] == "chunk_3"


def test_empty_index_search_returns_nothing() -> None:
    index = NumpyFlatIndex()
    assert index.search(np.zeros(8, dtype=np.float32), top_k=5) == []


def test_quantized_index_save_and_load_round_trip(tmp_path: Path) -> None:
    index = QuantizedNumpyFlatIndex()
    vectors = _random_vectors(10, 8, seed=2)
    ids = [f"chunk_{i}" for i in range(10)]
    index.add(ids, vectors)

    path = tmp_path / "quantized.npz"
    index.save(path)
    loaded = QuantizedNumpyFlatIndex.load(path)

    assert len(loaded) == len(index)
    assert loaded.search(vectors[3], top_k=1)[0][0] == "chunk_3"


def test_quantization_uses_roughly_a_quarter_of_the_memory() -> None:
    vectors = _random_vectors(500, 64, seed=4)
    ids = [f"chunk_{i}" for i in range(500)]

    flat = NumpyFlatIndex()
    flat.add(ids, vectors)
    quantized = QuantizedNumpyFlatIndex()
    quantized.add(ids, vectors)

    # float32 -> int8 for the vectors themselves is exactly a 4x reduction;
    # per-row min/scale overhead lives outside `.nbytes` (which measures
    # only the codes array), so the ratio should land almost exactly at 0.25.
    ratio = quantized.nbytes / flat.nbytes
    assert 0.2 <= ratio <= 0.3


def test_quantized_index_agrees_with_flat_index_on_top_result_most_of_the_time() -> None:
    vectors = _random_vectors(200, 32, seed=3)
    ids = [f"chunk_{i}" for i in range(200)]
    queries = _random_vectors(50, 32, seed=99)  # independent queries, not in the index

    flat = NumpyFlatIndex()
    flat.add(ids, vectors)
    quantized = QuantizedNumpyFlatIndex()
    quantized.add(ids, vectors)

    matches = sum(
        1
        for query in queries
        if flat.search(query, top_k=1)[0][0] == quantized.search(query, top_k=1)[0][0]
    )

    # measured at 50/50 on this seed; keep real margin below that so the
    # test isn't pinned to one exact run while still meaning something
    assert matches / len(queries) >= 0.85
