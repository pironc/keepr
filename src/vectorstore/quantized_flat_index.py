"""Scalar (int8) quantization for the flat index.

Same retrieval math as NumpyFlatIndex, but vectors are stored as int8
instead of float32 — roughly a 4x memory reduction, at the cost of a
small amount of approximation. Per-vector min/max scaling:
`scale = (max - min) / 255`, `code = round((v - min) / scale)` clipped to
[0, 255], stored as uint8 alongside its own (min, scale) for
dequantization at search time. This is exactly the same trade-off as the
LLM's own GGUF quantization, just applied to embedding vectors instead of
model weights — see ARCHITECTURE.md for the measured memory/latency/recall
comparison against the float32 index.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from src.vectorstore.similarity import l2_normalize_rows


class QuantizedNumpyFlatIndex:
    def __init__(self) -> None:
        self._ids: list[str] = []
        self._codes: NDArray[np.uint8] | None = None  # (n, dims)
        self._mins: NDArray[np.float32] | None = None  # (n,)
        self._scales: NDArray[np.float32] | None = None  # (n,)

    def __len__(self) -> int:
        return len(self._ids)

    def add(self, ids: list[str], vectors: NDArray[np.float32]) -> None:
        if len(ids) != vectors.shape[0]:
            raise ValueError("ids and vectors must have the same length")
        if not ids:
            return
        normalized = l2_normalize_rows(vectors.astype(np.float32, copy=False))
        codes, mins, scales = _quantize_rows(normalized)
        if self._codes is None or self._mins is None or self._scales is None:
            self._codes, self._mins, self._scales = codes, mins, scales
        else:
            self._codes = np.vstack([self._codes, codes])
            self._mins = np.concatenate([self._mins, mins])
            self._scales = np.concatenate([self._scales, scales])
        self._ids.extend(ids)

    def search(self, query: NDArray[np.float32], top_k: int) -> list[tuple[str, float]]:
        if self._codes is None or self._mins is None or self._scales is None or not self._ids:
            return []
        normalized_query = l2_normalize_rows(query.reshape(1, -1).astype(np.float32, copy=False))[0]
        dequantized = _dequantize_rows(self._codes, self._mins, self._scales)
        scores = dequantized @ normalized_query
        k = min(top_k, len(self._ids))
        if k <= 0:
            return []
        top_indices = np.argpartition(-scores, k - 1)[:k]
        top_indices = top_indices[np.argsort(-scores[top_indices])]
        return [(self._ids[i], float(scores[i])) for i in top_indices]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        codes = self._codes if self._codes is not None else np.zeros((0, 0), dtype=np.uint8)
        mins = self._mins if self._mins is not None else np.zeros((0,), dtype=np.float32)
        scales = self._scales if self._scales is not None else np.zeros((0,), dtype=np.float32)
        np.savez(path, codes=codes, mins=mins, scales=scales, ids=np.array(self._ids, dtype=object))

    @classmethod
    def load(cls, path: Path) -> QuantizedNumpyFlatIndex:
        index = cls()
        if not path.exists():
            return index
        with np.load(path, allow_pickle=True) as data:
            codes = data["codes"]
            ids = data["ids"].tolist()
            mins = data["mins"]
            scales = data["scales"]
        if codes.size:
            index._codes = codes.astype(np.uint8)
            index._mins = mins.astype(np.float32)
            index._scales = scales.astype(np.float32)
            index._ids = ids
        return index

    @property
    def nbytes(self) -> int:
        """Raw memory footprint of the stored codes — used by the quantization benchmark."""
        return 0 if self._codes is None else int(self._codes.nbytes)


def _quantize_rows(
    matrix: NDArray[np.float32],
) -> tuple[NDArray[np.uint8], NDArray[np.float32], NDArray[np.float32]]:
    row_min = matrix.min(axis=1)
    row_max = matrix.max(axis=1)
    span = row_max - row_min
    span[span == 0] = 1.0
    scale = span / 255.0
    codes = np.round((matrix - row_min[:, None]) / scale[:, None]).clip(0, 255).astype(np.uint8)
    return codes, row_min.astype(np.float32), scale.astype(np.float32)


def _dequantize_rows(
    codes: NDArray[np.uint8], mins: NDArray[np.float32], scales: NDArray[np.float32]
) -> NDArray[np.float32]:
    result: NDArray[np.float32] = (codes.astype(np.float32) * scales[:, None]) + mins[:, None]
    return result
