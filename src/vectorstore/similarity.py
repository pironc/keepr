"""Shared similarity math used by every VectorIndex implementation."""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray


def l2_normalize_rows(matrix: NDArray[np.float32]) -> NDArray[np.float32]:
    """Normalize each row to unit length, so a dot product equals cosine similarity."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms
