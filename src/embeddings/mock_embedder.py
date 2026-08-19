"""Deterministic embedder for tests and CI.

A hashing-trick bag-of-words vectorizer, not a neural embedding model.
It has no notion of language-model semantics, but it does preserve the
one real property a retrieval test needs: texts sharing more words score
more similar via cosine similarity than texts sharing none — and it needs
zero downloads, zero model weights, and runs in microseconds.
"""

from __future__ import annotations

import re
from hashlib import blake2b

import numpy as np
from numpy.typing import NDArray

_WORD_PATTERN = re.compile(r"[a-z0-9]+")


class MockEmbedder:
    def __init__(self, dimensions: int = 64) -> None:
        self.dimensions = dimensions

    async def embed_documents(self, texts: list[str]) -> NDArray[np.float32]:
        if not texts:
            return np.zeros((0, self.dimensions), dtype=np.float32)
        return np.stack([self._embed_one(text) for text in texts])

    async def embed_query(self, text: str) -> NDArray[np.float32]:
        return self._embed_one(text)

    async def availability(self) -> str | None:
        """The mock embedder is always usable — it needs no model file."""
        return None

    def _embed_one(self, text: str) -> NDArray[np.float32]:
        vector = np.zeros(self.dimensions, dtype=np.float32)
        for word in _WORD_PATTERN.findall(text.lower()):
            index = int.from_bytes(blake2b(word.encode("utf-8"), digest_size=4).digest(), "big")
            vector[index % self.dimensions] += 1.0
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm > 0 else vector
