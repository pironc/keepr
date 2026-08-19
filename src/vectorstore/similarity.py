"""Shared similarity math used by every VectorIndex implementation."""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray


def l2_normalize_rows(matrix: NDArray[np.float32]) -> NDArray[np.float32]:
    """Normalize each row to unit length, so a dot product equals cosine similarity."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def select_top_k(
    ids: list[str], scores: NDArray[np.float32], top_k: int
) -> list[tuple[str, float]]:
    """Return the top_k (id, score) pairs, sorted by descending score.

    Uses `np.argpartition`, an O(n) selection instead of a full O(n log n)
    sort — only the k largest scores are needed, not a full ranking.
    """
    k = min(top_k, len(ids))
    if k <= 0:
        return []
    top_indices = np.argpartition(-scores, k - 1)[:k]
    top_indices = top_indices[np.argsort(-scores[top_indices])]
    return [(ids[i], float(scores[i])) for i in top_indices]
