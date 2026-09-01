"""Serializes access to the shared llama.cpp model instances.

The installed llama_cpp source (`.venv/lib/python3.13/
site-packages/llama_cpp/llama.py`) has no internal locking anywhere in the
`Llama` class — both `embed()` and `generate()` mutate substantial
non-atomic instance state (batch reset -> accumulate -> decode -> reset).
`IngestionPipeline`'s embedding calls and `RagEngine`'s query embedding
call run on the same singleton `LlamaCppEmbedder` with zero
synchronization today: a file upload embedding chunks while a question is
being embedded for retrieval can race and corrupt shared model state,
independent of anything else.

Solved structurally, not with a lock added at today's 2-3 call sites: wrap
the embedder and driver once, at construction time, so every future call
site is forced through the same lock automatically.

Each wrapper gets its own lock (see app.py), not a single shared lock.
A shared lock would block the embedder for the entire duration of an LLM
token-generation stream — potentially 30+ seconds — just to run a batch
embedding pass that takes ~1-2 s.  By default the embedder runs on CPU
(EMBEDDING_GPU_LAYERS=0) so the two Llama instances never contend for
the same GPU device; the separate locks are correct and concurrent
embedding-during-generation works as intended.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import cast

import numpy as np
from numpy.typing import NDArray

from src.embeddings.base import Embedder
from src.llm.base import LLMDriver
from src.models import LLMMessage


class LockedEmbedder:
    def __init__(self, inner: Embedder, lock: asyncio.Lock) -> None:
        self._inner = inner
        self._lock = lock
        self.dimensions = inner.dimensions

    async def embed_documents(self, texts: list[str]) -> NDArray[np.float32]:
        async with self._lock:
            return await self._inner.embed_documents(texts)

    async def embed_query(self, text: str) -> NDArray[np.float32]:
        async with self._lock:
            return await self._inner.embed_query(text)

    async def availability(self) -> str | None:
        """Cheap sibling-probe (no model load) forwarded to the inner embedder."""
        return await self._inner.availability()

    async def set_model_path(self, new_path: Path) -> None:
        """Live-swap the embedding model under the lock — can't race an in-flight
        embedding pass (same guarantee as embed_documents/embed_query)."""
        set_path = getattr(self._inner, "set_model_path", None)
        if set_path is None:
            return
        async with self._lock:
            await asyncio.to_thread(set_path, new_path)
        # Keep the wrapper's cached dimension in sync so the RAG engine / guard
        # see the new width.
        self.dimensions = self._inner.dimensions

    def model_path(self) -> Path:
        """Current embedding model file (read-only; no lock needed)."""
        p = getattr(self._inner, "model_path", None)
        return p() if p is not None else Path()

    async def unload(self) -> None:
        """Free the inner model (e.g. after an idle timeout)."""
        unload = getattr(self._inner, "unload", None)
        if unload is not None:
            async with self._lock:
                await asyncio.to_thread(unload)

    async def aclose(self) -> None:
        """Shutdown cleanup for the inner model."""
        aclose = getattr(self._inner, "aclose", None)
        if aclose is not None:
            async with self._lock:
                await aclose()


class LockedLLMDriver(LLMDriver):
    def __init__(self, inner: LLMDriver, lock: asyncio.Lock) -> None:
        self._inner = inner
        self._lock = lock

    async def generate(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        # aclosing, not a bare `async for` — an abandoned/errored consumer
        # must still release the lock, or a bug elsewhere could leave every
        # later call blocked forever with no obvious top-level error.
        # LLMDriver.generate is declared as the broader AsyncIterator[str],
        # but every concrete implementation (MockLLMDriver, LlamaCppDriver)
        # is actually an async-generator function, which is what aclosing
        # requires — narrowing here, not upstream, is the same targeted-cast
        # pattern already used in llama_cpp_driver.py.
        inner_stream = cast(AsyncGenerator[str, None], self._inner.generate(messages))
        async with self._lock, contextlib.aclosing(inner_stream) as stream:
            async for token in stream:
                yield token

    async def availability(self) -> str | None:
        """Cheap sibling-probe (no model load) forwarded to the inner driver."""
        return await self._inner.availability()

    async def set_model_path(self, new_path: Path) -> None:
        """Live-swap the LLM under the lock — can't race an in-flight generation.
        The inner driver exposes a synchronous ``set_model_path`` (file-backed
        llama.cpp), so it's delegated the same way ``unload`` is."""
        set_path = getattr(self._inner, "set_model_path", None)
        if set_path is None:
            return
        async with self._lock:
            await asyncio.to_thread(set_path, new_path)

    def model_path(self) -> Path:
        """Current LLM file this driver is pointed at (read-only; no lock needed)."""
        p = getattr(self._inner, "model_path", None)
        return p() if p is not None else Path()

    async def unload(self) -> None:
        """Free the inner model (e.g. after an idle timeout)."""
        unload = getattr(self._inner, "unload", None)
        if unload is not None:
            async with self._lock:
                await asyncio.to_thread(unload)

    async def aclose(self) -> None:
        """Shutdown cleanup for the inner model."""
        aclose = getattr(self._inner, "aclose", None)
        if aclose is not None:
            async with self._lock:
                await aclose()
