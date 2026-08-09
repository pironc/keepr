"""Embedder protocol: turns text into vectors for indexing and querying.

Document and query embedding are separate methods, not one call — some
embedding models (nomic-embed-text-v1.5 included) are asymmetric bi-encoders
trained with different prefixes for documents vs. queries, and conflating
the two silently degrades retrieval quality without ever raising an error.
"""

from __future__ import annotations

from typing import Protocol

import numpy as np
from numpy.typing import NDArray


class Embedder(Protocol):
    dimensions: int

    async def embed_documents(self, texts: list[str]) -> NDArray[np.float32]: ...

    async def embed_query(self, text: str) -> NDArray[np.float32]: ...
