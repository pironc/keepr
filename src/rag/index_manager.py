"""Loads, caches, and persists the per-conversation VectorIndex.

One flat index per conversation keeps retrieval scoped to only the files
you actually attached to that chat. A single lock serializes mutations —
more than enough for a single local user, and it avoids any chance of two
concurrent ingests corrupting the same on-disk index.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from src.vectorstore.base import VectorIndex
from src.vectorstore.factory import load_index


class IndexManager:
    def __init__(self, index_dir: Path, backend: str) -> None:
        self._index_dir = index_dir
        self._backend = backend
        self._cache: dict[str, VectorIndex] = {}
        self._lock = asyncio.Lock()

    def _path_for(self, conversation_id: str) -> Path:
        return self._index_dir / f"{conversation_id}.npz"

    async def get(self, conversation_id: str) -> VectorIndex:
        async with self._lock:
            if conversation_id not in self._cache:
                path = self._path_for(conversation_id)
                self._cache[conversation_id] = await asyncio.to_thread(load_index, self._backend, path)
            return self._cache[conversation_id]

    async def save(self, conversation_id: str) -> None:
        async with self._lock:
            index = self._cache.get(conversation_id)
            if index is not None:
                await asyncio.to_thread(index.save, self._path_for(conversation_id))
