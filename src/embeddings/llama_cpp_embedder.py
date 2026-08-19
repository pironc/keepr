"""Embeddings via a GGUF embedding model through llama-cpp-python.

nomic-embed-text-v2-moe (multilingual, ~100 languages — swapped in for
v1.5, which was English-only) keeps the same asymmetric bi-encoder
convention as v1.5: documents and queries must be embedded with different
prefixes (`search_document:` / `search_query:`) for the similarity scores
to be meaningful — this isn't a stylistic quirk, it's how the model was
trained, and getting it wrong silently degrades retrieval quality without
raising any error. Same prefixes, same default 768 dimensions, so this
swap needed no code changes here beyond this comment.

The model is loaded lazily on first embedding, not at startup — a GGUF
file can be gigabytes; deferring the load means the app starts instantly
and uses no model RAM until the first document or query actually embeds.
Loading runs inside `_run_sync_uncancellable` (same thread as embedding),
so cancellation safety and the LockedEmbedder lock are both preserved.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from src.logger import get_logger
from src.model_unavailable import ModelRole, ModelUnavailableError

logger = get_logger(__name__)

_MISSING_MSG_LEAD = "Embedding model file not found"
_LOAD_MSG_LEAD = "Embedding model could not be loaded — the file may be corrupted or the wrong architecture for this build of keepr"

_DOCUMENT_PREFIX = "search_document: "
_QUERY_PREFIX = "search_query: "

# nomic-embed-text-v2-moe always produces 768-dimensional embeddings —
# hardcoded so we can report dimensions without loading the model eagerly.
_NOMIC_EMBED_DIMENSIONS = 768


class LlamaCppEmbedder:
    def __init__(self, model_path: Path, n_gpu_layers: int = -1) -> None:
        self._model_path = model_path
        self._n_gpu_layers = n_gpu_layers
        self._model: Any = None
        self.dimensions: int = _NOMIC_EMBED_DIMENSIONS

    async def embed_documents(self, texts: list[str]) -> NDArray[np.float32]:
        prefixed = [f"{_DOCUMENT_PREFIX}{text}" for text in texts]
        return await _run_sync_uncancellable(self._embed_sync, prefixed)

    async def embed_query(self, text: str) -> NDArray[np.float32]:
        result = await _run_sync_uncancellable(self._embed_sync, [f"{_QUERY_PREFIX}{text}"])
        first_row: NDArray[np.float32] = result[0]
        return first_row

    def _embed_sync(self, texts: list[str]) -> NDArray[np.float32]:
        if self._model is None:
            self._load()
        embeddings = self._model.embed(texts)
        return np.asarray(embeddings, dtype=np.float32)

    def _load(self) -> None:
        """Load the GGUF model into memory (called inside _embed_sync, which
        runs in the _run_sync_uncancellable thread — cancellation-safe).

        Any load-time failure (missing file, corrupted/truncated GGUF, wrong
        architecture, OOM) is surfaced as a :class:`ModelUnavailableError` —
        never a raw llama-cpp exception — so the ingestion pipeline and RAG
        engine can turn it into a readable, actionable error instead of
        crashing the SSE stream."""
        from llama_cpp import Llama  # lazy: only needed when this embedder is actually selected

        if not self._model_path.is_file():
            raise ModelUnavailableError(
                f"{_MISSING_MSG_LEAD}: {self._model_path}. "
                "Download it in Settings → Models, or copy a .gguf file into it.",
                role=ModelRole.EMBEDDING,
            )
        logger.info("llama_cpp: loading embedder %s …", self._model_path.name)
        try:
            self._model = Llama(
                model_path=str(self._model_path), embedding=True,
                n_gpu_layers=self._n_gpu_layers, verbose=False,
            )
        except Exception as exc:  # corrupt/truncated/wrong-gauge load failures
            raise ModelUnavailableError(
                f"{_LOAD_MSG_LEAD}: {self._model_path.name}. "
                "Try re-downloading the model.",
                role=ModelRole.EMBEDDING,
            ) from exc
        logger.info("llama_cpp: embedder loaded")

    async def availability(self) -> str | None:
        """Cheap availability check (no model load): the embedder is usable
        only if its GGUF file exists on disk.  A file that exists but later
        fails to load is *not* reported here — that is only discoverable at
        embed time, when the load-time ``ModelUnavailableError`` carries the
        specific corrupt/wrong-architecture reason."""
        if not self._model_path.is_file():
            return (
                f"{_MISSING_MSG_LEAD}: {self._model_path.name}. "
                "Download it in Settings → Models, or copy a .gguf file into it."
            )
        return None

    def unload(self) -> None:
        """Free the model (e.g. after an idle timeout).  Safe to call multiple times."""
        if self._model is not None:
            logger.info("llama_cpp: unloading embedder")
            self._model.close()
            self._model = None

    async def aclose(self) -> None:
        """Shutdown cleanup."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.unload)


async def _run_sync_uncancellable[T](func: Callable[..., T], *args: Any) -> T:
    """Run *func* in a thread-pool thread, suppressing asyncio cancellation
    until the thread finishes.

    ``asyncio.to_thread`` is a one-shot await: once cancelled the future
    is abandoned but the underlying thread keeps running — there is no way
    to re-attach to the abandoned result.  If this future sits behind an
    ``asyncio.Lock`` (e.g. inside ``LockedEmbedder``), the lock is released
    on cancellation while the thread is still touching a shared llama.cpp
    model; the next caller then acquires the lock and enters the same
    model from a second thread simultaneously, corrupting internal state
    (observed as an GGML_ASSERT crash in ggml-cpu/repack.cpp).

    Two cancellation subtleties:

    1. **shield.** ``Task.cancel()`` also calls ``self._fut_waiter.cancel()``
       on the inner future from ``run_in_executor``, permanently cancelling
       it.  Re-awaiting a cancelled future raises ``CancelledError``
       *immediately* on every attempt, creating a tight infinite spin-loop
       that starves the entire event loop.  ``asyncio.shield()`` lets the
       ``CancelledError`` through (so we can catch + uncancel the task)
       while keeping the inner future alive for re-awaiting.

    2. **loop.** Starlette's streaming-response cleanup can fire
       ``task.cancel()`` *more than once* (disconnect handler, then the
       ``finally`` block around ``task_group``).  Each additional cancel
       must also be suppressed — otherwise it escapes through
       ``LockedEmbedder.__aexit__``, releases the lock while the thread is
       still in the model, and the very next caller walks straight into a
       two-thread-same-model concurrency crash.  The ``while True`` loop
       catches every one of them.
    """
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(None, func, *args)
    task = asyncio.current_task()
    while True:
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if task is not None:
                task.uncancel()
            # Loop back — shield protected the future from being cancelled,
            # so it's still alive.  Re-enter the await; the thread (and the
            # lock held by the caller) stay alive until it completes.
            continue
