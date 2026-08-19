"""Real local inference via llama-cpp-python: GGUF, Metal/CUDA/CPU from one codebase.

llama-cpp-python's streaming chat completion is a *synchronous* generator —
each `next()` call runs real inference and blocks. Bridging that into an
async generator without blocking the event loop needs a background thread
handing tokens back through a thread-safe queue; a naive `async def` wrapper
around the sync generator would stall every other concurrent request for
the duration of generation.

Reasoning models served this way (e.g. Qwen3) default to opening every
response with a literal, real `<think>...</think>` block of raw
chain-of-thought — not a template artifact the caller can ignore, actual
generated text that would otherwise reach the RAG engine, get persisted,
and render to the user verbatim. `_strip_thinking` filters it out of the
token stream itself, the one place that's true regardless of caller.

The model is loaded lazily on first inference, not at startup — a GGUF
file can be gigabytes; deferring the load means the app starts instantly
and uses no model RAM until the first query actually needs it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from src.llm.base import LLMDriver
from src.logger import get_logger
from src.model_unavailable import ModelRole, ModelUnavailableError
from src.models import LLMMessage

logger = get_logger(__name__)

_MISSING_MSG_LEAD = "Language model file not found"
_LOAD_MSG_LEAD = "Language model could not be loaded — the file may be corrupted or the wrong architecture for this build of keepr"


class LlamaCppDriver(LLMDriver):
    def __init__(self, model_path: Path, n_ctx: int, n_gpu_layers: int = -1) -> None:
        self._model_path = model_path
        self._n_ctx = n_ctx
        self._n_gpu_layers = n_gpu_layers
        self._model: Any = None

    def _load(self) -> None:
        """Load the GGUF model into memory (called inside the produce() thread).
        Kept as a separate method so unload() + re-load works cleanly.

        Any load-time failure (missing file, corrupted/truncated GGUF, wrong
        architecture, OOM) is surfaced as a :class:`ModelUnavailableError` —
        never a raw llama-cpp exception — so callers can turn it into a
        readable, actionable error rather than an opaque worker failure."""
        from llama_cpp import Llama  # lazy: only needed when this driver is actually selected

        if not self._model_path.is_file():
            raise ModelUnavailableError(
                f"{_MISSING_MSG_LEAD}: {self._model_path}. "
                "Download it in Settings → Models, or copy a .gguf file into it.",
                role=ModelRole.LANGUAGE,
            )
        logger.info("llama_cpp: loading LLM %s (n_ctx=%d, n_gpu_layers=%d) …",
                     self._model_path.name, self._n_ctx, self._n_gpu_layers)
        try:
            self._model = Llama(
                model_path=str(self._model_path), n_ctx=self._n_ctx,
                n_gpu_layers=self._n_gpu_layers, verbose=False,
            )
        except Exception as exc:  # corrupt/truncated/wrong-gauge load failures
            raise ModelUnavailableError(
                f"{_LOAD_MSG_LEAD}: {self._model_path.name}. "
                "Try re-downloading the model.",
                role=ModelRole.LANGUAGE,
            ) from exc
        logger.info("llama_cpp: LLM loaded")

    async def availability(self) -> str | None:
        """Cheap availability check (no model load): the language model is
        usable only if its GGUF file exists on disk.  A file that exists but
        later fails to load is *not* reported here — that is only discoverable
        at generate time, when the load-time ``ModelUnavailableError`` carries
        the specific corrupt/wrong-architecture reason."""
        if not self._model_path.is_file():
            return (
                f"{_MISSING_MSG_LEAD}: {self._model_path.name}. "
                "Download it in Settings → Models, or copy a .gguf file into it."
            )
        return None

    def unload(self) -> None:
        """Free the model (e.g. after an idle timeout).  Safe to call multiple times."""
        if self._model is not None:
            logger.info("llama_cpp: unloading LLM")
            self._model.close()
            self._model = None

    async def aclose(self) -> None:
        """Shutdown cleanup."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.unload)

    async def generate(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        # llama-cpp-python's typed message overloads (system/user/assistant/tool/
        # function messages) are more specific than we need here; our dicts are
        # runtime-correct (role + content) but not worth hand-typing per-role.
        wire_messages: Any = [
            {"role": message.role, "content": message.content} for message in messages
        ]
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()

        def produce() -> None:
            error: Exception | None = None
            try:
                # Lazy-load the model on first use — the produce() thread is
                # protected by the same cancellation handling as inference:
                # LockedLLMDriver holds its lock through cleanup, and the
                # async wrapper below awaits the future in its finally block.
                if self._model is None:
                    self._load()
                stream: Any = self._model.create_chat_completion(messages=wire_messages, stream=True)
                for chunk in stream:
                    token = _extract_token(chunk)
                    if token:
                        loop.call_soon_threadsafe(queue.put_nowait, token)
            except Exception as exc:
                # A load/inference failure must NOT be silently swallowed —
                # otherwise a missing or corrupt model yields an empty stream
                # (the thread's exception dies with it and the queue just gets
                # a None sentinel) and the caller sees a blank answer with no
                # explanation.  Stash it so the async side re-raises the real
                # cause.
                error = exc
            finally:
                # Normal end-of-stream is the bare `None` sentinel; an error
                # rides along as the exception itself so raw_tokens re-raises it.
                loop.call_soon_threadsafe(queue.put_nowait, error if error is not None else None)

        async def raw_tokens() -> AsyncIterator[str]:
            future = loop.run_in_executor(None, produce)
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    if isinstance(item, Exception):
                        raise item
                    yield item
            finally:
                await future

        async for token in _strip_thinking(raw_tokens()):
            yield token


def _extract_token(chunk: dict[str, Any]) -> str | None:
    delta = chunk["choices"][0]["delta"]
    content = delta.get("content")
    return content if isinstance(content, str) else None


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


async def _strip_thinking(tokens: AsyncIterator[str]) -> AsyncIterator[str]:
    """Drop a leading <think>...</think> block from the stream entirely —
    never yield so much as a partial tag. Buffers only while it's still
    ambiguous whether the response opens with one (tokenization can split
    "<think>" across several chunks); once resolved either way, normal
    content passes through per-token with no added latency.
    """
    buffer = ""
    in_think = False
    resolved = False
    async for token in tokens:
        buffer += token
        if not resolved:
            stripped = buffer.lstrip()
            if stripped.startswith(_THINK_OPEN):
                resolved = True
                in_think = True
                buffer = stripped[len(_THINK_OPEN) :]
            elif _THINK_OPEN.startswith(stripped):
                continue  # still an unambiguous prefix of "<think>" (or all whitespace so far)
            else:
                resolved = True  # diverged from "<think>": definitely not a think block

        if in_think:
            close_at = buffer.find(_THINK_CLOSE)
            if close_at == -1:
                continue
            buffer = buffer[close_at + len(_THINK_CLOSE) :].lstrip()
            in_think = False

        if buffer:
            yield buffer
            buffer = ""
