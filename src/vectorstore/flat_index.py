"""Exact cosine-similarity search over a dense in-memory matrix.

The whole trick: L2-normalize every vector once at insert time, so cosine
similarity between any two vectors reduces to a single dot product
(`matrix @ query`). Top-k uses `np.argpartition`, an O(n) selection
instead of a full O(n log n) sort — you don't need the full ranking,
only the k largest scores.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from src.vectorstore.similarity import l2_normalize_rows


class NumpyFlatIndex:
    def __init__(self) -> None:
        self._ids: list[str] = []
        self._vectors: NDArray[np.float32] | None = None  # (n, dims), L2-normalized rows

    def __len__(self) -> int:
        return len(self._ids)

    def add(self, ids: list[str], vectors: NDArray[np.float32]) -> None:
        if len(ids) != vectors.shape[0]:
            raise ValueError("ids and vectors must have the same length")
        if not ids:
            return
        normalized = l2_normalize_rows(vectors.astype(np.float32, copy=False))
        self._vectors = normalized if self._vectors is None else np.vstack([self._vectors, normalized])
        self._ids.extend(ids)

    def search(self, query: NDArray[np.float32], top_k: int) -> list[tuple[str, float]]:
        if self._vectors is None or not self._ids:
            return []
        normalized_query = l2_normalize_rows(query.reshape(1, -1).astype(np.float32, copy=False))[0]
        scores = self._vectors @ normalized_query
        k = min(top_k, len(self._ids))
        if k <= 0:
            return []
        top_indices = np.argpartition(-scores, k - 1)[:k]
        top_indices = top_indices[np.argsort(-scores[top_indices])]
        return [(self._ids[i], float(scores[i])) for i in top_indices]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        vectors = self._vectors if self._vectors is not None else np.zeros((0, 0), dtype=np.float32)
        np.savez(path, vectors=vectors, ids=np.array(self._ids, dtype=object))

    @classmethod
    def load(cls, path: Path) -> NumpyFlatIndex:
        index = cls()
        if not path.exists():
            return index
        with np.load(path, allow_pickle=True) as data:
            vectors = data["vectors"]
            ids = data["ids"].tolist()
        if vectors.size:
            index._vectors = vectors.astype(np.float32)
            index._ids = ids
        return index

    @property
    def nbytes(self) -> int:
        """Raw memory footprint of the stored vectors — used by the quantization benchmark."""
        return 0 if self._vectors is None else int(self._vectors.nbytes)
