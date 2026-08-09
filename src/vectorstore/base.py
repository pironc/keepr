"""VectorIndex protocol: the retrieval boundary.

Both concrete implementations here are hand-rolled — brute-force cosine
similarity over a personal-scale corpus (thousands of chunks) is exact
and sub-millisecond, and owning this math end-to-end is the point of
this project. See ARCHITECTURE.md for the measured threshold past which
you'd swap this for an ANN index instead — the interface is designed so
that swap only ever touches this one module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import numpy as np
from numpy.typing import NDArray


class VectorIndex(Protocol):
    def __len__(self) -> int: ...

    def add(self, ids: list[str], vectors: NDArray[np.float32]) -> None: ...

    def search(self, query: NDArray[np.float32], top_k: int) -> list[tuple[str, float]]: ...

    def save(self, path: Path) -> None: ...
