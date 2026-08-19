"""Model-download helpers shared between the CLI script and the API route.

The standalone ``scripts/download_models.py`` CLI imports these constants and
the hash/verify helpers from here rather than re-defining them, so the model
catalog lives in exactly one place — the CLI and the in-app
``/api/models/download`` route can never silently disagree about what to fetch.
"""

from __future__ import annotations

import asyncio
import hashlib
import multiprocessing as mp
import os
import queue as _queue_lib
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol

from src.logger import get_logger


def _hub_token() -> str | None:
    """Return the user's Hugging Face token, if they've configured one.

    keepr is privacy-first: authentication with the Hub is voluntary and never
    required for the default public models.  If ``HF_TOKEN`` is exported in the
    environment (the standard ``huggingface_hub`` way), we pass it through so
    downloads get higher rate limits and the Hub stops printing the
    "unauthenticated requests" warning.  An empty value counts as unset so an
    env var explicitly blanked out behaves the same as absent.  With no token,
    public-model downloads still work — just unauthenticated.
    """
    token = os.environ.get("HF_TOKEN", "").strip()
    return token or None


class ProgressSink(Protocol):
    """The smallest queue surface ``_DownloadProgressTqdm`` needs.

    Both an ``asyncio.Queue`` (in-process) and a ``multiprocessing.Queue``
    (child-process download worker) satisfy it, so progress can be pushed from
    whichever side the download runs on without dragging queue-specific types
    through the class.
    """

    def put_nowait(self, item: dict[str, object]) -> None: ...


def _as_float(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _as_int(value: object) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    return 0


logger = get_logger(__name__)

LLM_REPO_ID = "bartowski/Qwen_Qwen3-8B-GGUF"
LLM_FILENAME = "Qwen_Qwen3-8B-Q6_K.gguf"

EMBEDDING_REPO_ID = "nomic-ai/nomic-embed-text-v2-moe-GGUF"
EMBEDDING_FILENAME = "nomic-embed-text-v2-moe.Q8_0.gguf"

# Map used by the /api/models/download endpoint to resolve "llm"/"embedding" keys.
MODEL_DEFS: dict[str, tuple[str, str]] = {
    "llm": (LLM_REPO_ID, LLM_FILENAME),
    "embedding": (EMBEDDING_REPO_ID, EMBEDDING_FILENAME),
}


def sha256_of(path: Path, chunk_size: int = 1 << 20) -> str:
    """Streamed so multi-GB GGUF files never sit fully in memory at once."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_sha256(repo_id: str, filename: str) -> str:
    """Authoritative upstream digest from the Hub API's LFS metadata."""
    from huggingface_hub import HfApi

    info = HfApi(token=_hub_token()).model_info(repo_id, files_metadata=True)
    siblings = info.siblings or []
    sibling = next((s for s in siblings if s.rfilename == filename), None)
    if sibling is None or sibling.lfs is None:
        raise RuntimeError(
            f"no LFS sha256 metadata for {filename!r} in {repo_id} — can't verify"
        )
    return sibling.lfs.sha256


class _DownloadProgressTqdm:
    """Minimal tqdm-compatible wrapper that pushes progress to an asyncio.Queue.

    ``huggingface_hub.hf_hub_download`` accepts a ``tqdm_class`` callable; we
    return an instance of this class instead of the real ``tqdm`` so that every
    ``update()`` call pushes a ``(progress, downloaded_bytes, total_bytes)``
    tuple into the queue.  The async SSE generator on the other side reads
    from the queue and yields events.

    Modern ``huggingface_hub`` downloads through the xet backend, which routes
    progress through an ``XetDownloadProgressReporter``.  That reporter decides
    whether one bar or two by inspecting the ``tqdm_class`` you pass it:

    * if the class exposes ``update_transfer``, it aggregates both the network
      transfer and the reconstruction phases onto *one* bar — the one we hand
      back — so we see live byte counts the whole way;
    * otherwise it keeps a second, separate real-``tqdm`` transfer bar (the
      terminal's "Downloading bytes…" line) that we never see, and our bar only
      receives the delayed reconstruction flush at the very end.

    That second case is exactly the bug that made the app sit on
    "Verifying model…": the load went into a terminal-only bar and our queue saw
    nothing until the file was fully buffered.  So this class must be handed to
    ``hf_hub_download`` *as the class* (``tqdm_class=_DownloadProgressTqdm``), not
    hidden behind a factory lambda — a lambda has no ``update_transfer``
    attribute, so the reporter can't detect the aggregation hook and falls back
    to the two-bar layout.

    Because ``hf_hub_download`` constructs the bar itself (without our ``queue``
    kwarg), each download sets ``_DownloadProgressTqdm._current_queue`` on the
    class right before launching its worker; the bar picks it up from there.
    The download thread and the one-at-a-time download guard (frontend + CLI
    are both sequential) make this single class-level slot safe.

    The reporter also expects more of the tqdm surface than ``update`` alone:

    * ``update(inc)`` — reconstruction bytes flushed to disk;
    * ``update_transfer(inc)`` — network bytes received;
    * ``set_postfix_str`` / ``set_transfer_postfix_str`` / ``refresh`` —
      console-render calls that do nothing here (the numbers travel via the
      SSE queue, there is no terminal render);
    * ``format_dict`` — a stub attribute the reporter reads for its speed
      postfix.

    Both phases call into the same counter so progress climbs with network
    bytes first and then sits at 100% while reconstruction catches up.
    """

    # Slotted on the class (not the instance) so a real tqdm-compatible class
    # (with ``update_transfer``, enabling xet aggregation) can be constructed by
    # ``hf_hub_download`` itself, which doesn't know about our ``queue`` kwarg.
    _current_queue: ProgressSink | None = None

    def __init__(
        self,
        *args: object,
        queue: ProgressSink | None = None,
        total: int = 0,
        **kwargs: object,
    ) -> None:
        kwargs.pop("name", None)  # hf_hub_download passes `name` — tqdm rejects it
        kwargs.pop("disable", None)
        resolved_queue = queue or _DownloadProgressTqdm._current_queue
        if resolved_queue is None:
            raise RuntimeError("_DownloadProgressTqdm used before a download set a queue")
        self._queue: ProgressSink = resolved_queue
        self.total = total
        self.n = 0
        # The xet reporter reads ``format_dict.get("rate")`` to build a speed
        # postfix; give it a dict so that read never raises.
        self.format_dict: dict[str, object] = {}

    def _push(self) -> None:
        self.n = min(self.n, self.total)  # never overshoot during reconstruction
        if self.total and self.total > 0:
            # Tagged dict (not a bare tuple) so the consumer can tell progress
            # items apart from the terminal {"type": "ok"/"error"} result.
            self._queue.put_nowait(
                {
                    "type": "progress",
                    "progress": self.n / self.total,
                    "downloaded": self.n,
                    "total": self.total,
                }
            )

    def update(self, n: int = 1) -> None:
        self.n += n
        self._push()

    def update_transfer(self, inc: int = 1) -> None:
        # Network bytes received (xet aggregated mode). Increments the same
        # counter so the fraction reflects live transfer progress, not just the
        # delayed reconstruction flush.
        self.n += inc
        self._push()

    def set_postfix_str(self, *args: object, **kwargs: object) -> None:
        pass

    def set_transfer_postfix_str(self, *args: object, **kwargs: object) -> None:
        pass

    def refresh(self, *args: object, **kwargs: object) -> None:
        pass

    def clear(self, *args: object, **kwargs: object) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self) -> _DownloadProgressTqdm:
        return self

    def __exit__(self, *args: object) -> None:
        pass


def _download_in_process(
    repo_id: str,
    filename: str,
    models_dir: str,
    progress_queue: ProgressSink,
) -> None:
    """Run ``hf_hub_download`` in a child process.

    ``hf_hub_download`` holds a blocking file lock for the whole transfer
    (``WeakFileLock`` on the cache blob).  If a client cancels and we merely
    orphan the old approach's executor *thread*, that lock stays held until the
    full model finishes — so a re-download of the same model blocks on the lock
    and the UI sits on "Verifying model…" forever.  Running the download in its
    own process lets the parent ``terminate()`` it on cancel, releasing the lock
    immediately and keeping a re-download responsive.

    This must be a module-level, importable callable so ``multiprocessing``
    ``spawn`` (the macOS default) can re-import it cleanly in the child.
    """
    from huggingface_hub import hf_hub_download

    Path(models_dir).mkdir(parents=True, exist_ok=True)
    # This lives in the child process; point the singleton at the mp queue the
    # parent handed us so hf_hub_download's tqdm pushes into it.  The token (if
    # any) is read from the inherited environment inside the worker so an
    # authenticated user gets higher rate limits and no "unauthenticated
    # requests" warning.
    _DownloadProgressTqdm._current_queue = progress_queue
    try:
        hf_hub_download(
            repo_id=repo_id,
            filename=filename,
            local_dir=Path(models_dir),
            tqdm_class=_DownloadProgressTqdm,
            token=_hub_token(),
        )
    except Exception as exc:
        progress_queue.put_nowait({"type": "error", "message": str(exc)})
    else:
        progress_queue.put_nowait({"type": "ok"})


def _download_feed_worker(
    progress_queue: Any,
    out_q: Any,
    loop: Any,
    stop: threading.Event,
) -> None:
    """Bridge the blocking multiprocessing queue into the event loop.

    Runs on a plain daemon thread: ``mp.Queue.get`` is a blocking call that
    would stall the event loop, so we relay each item onto an ``asyncio.Queue``
    via ``call_soon_threadsafe``.  The ``stop`` event lets the generator's
    teardown (``finally``) pull the thread down within one poll interval.
    """
    while not stop.is_set():
        try:
            item = progress_queue.get(timeout=0.2)
        except _queue_lib.Empty:
            continue
        except (OSError, EOFError):
            break
        loop.call_soon_threadsafe(out_q.put_nowait, item)


async def download_model_with_progress(
    model_key: str, models_dir: Path
) -> AsyncIterator[dict[str, object]]:
    """Download a model into ``models_dir``, yielding SSE-ready progress dicts."""

    if model_key not in MODEL_DEFS:
        yield {
            "model": model_key,
            "status": "error",
            "progress": 0,
            "message": f"Unknown model key: {model_key!r}",
        }
        return

    repo_id, filename = MODEL_DEFS[model_key]
    target = models_dir / filename

    # Phase 1: fetch the expected SHA256 from the Hub
    yield {
        "model": model_key,
        "status": "verifying",
        "progress": 0,
        "message": "Checking upstream SHA256…",
    }
    loop = asyncio.get_running_loop()
    expected = await loop.run_in_executor(None, expected_sha256, repo_id, filename)

    if target.exists():
        local = await loop.run_in_executor(None, sha256_of, target)
        if local == expected:
            yield {
                "model": model_key,
                "status": "already_exists",
                "progress": 1.0,
                "message": "Already installed and verified.",
            }
            return
        logger.info(
            "model_download: %s exists but SHA256 mismatch — re-downloading", filename
        )

    # Phase 2: run the download in a *child process* (not a thread) so it can be
    # terminated on cancel.  hf_hub_download holds a blocking file lock for the
    # whole transfer; a thread orphaned by a client disconnect would keep that
    # lock and block any re-download of the same model forever (the UI would sit
    # on "Verifying model…").  The child pushes progress into an mp queue, which
    # a feeder thread relays onto an asyncio.Queue for the SSE loop.  Pass the
    # tqdm-class itself (not a lambda wrapping it) so xet's reporter sees the
    # ``update_transfer`` aggregation hook and routes both transfer and
    # reconstruction onto the one bar — see _DownloadProgressTqdm's docstring.
    await loop.run_in_executor(None, lambda: models_dir.mkdir(parents=True, exist_ok=True))
    progress_queue: ProgressSink = mp.Queue()
    out_q: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    stop_event = threading.Event()
    proc = mp.Process(
        target=_download_in_process,
        args=(repo_id, filename, str(models_dir), progress_queue),
        daemon=True,
    )
    proc.start()
    feeder = threading.Thread(
        target=_download_feed_worker,
        args=(progress_queue, out_q, loop, stop_event),
        daemon=True,
    )
    feeder.start()

    error_msg: str | None = None
    ok = False
    # Flips once the shared transfer/reconstruction counter (see
    # _DownloadProgressTqdm's docstring) first reaches 1.0. xet's reporter
    # routes both network bytes and local reconstruction (reassembling
    # content-addressed chunks into the final file) through that one counter,
    # and reconstruction can lag visibly behind a fast transfer on a
    # multi-GB file — so hitting 1.0 here does not mean hf_hub_download is
    # done, only that the network side is. Surface that gap as its own
    # "finalizing" status once, instead of silently repeating "downloading
    # 100%" for however long reconstruction takes before the child finally
    # reports "ok" and Phase 3 (SHA256 verification) begins below.
    reached_full_progress = False
    try:
        while True:
            try:
                item = await asyncio.wait_for(out_q.get(), timeout=0.2)
            except TimeoutError:
                # No bytes this tick.  If the child died without sending a
                # result, treat it as an abnormal end (e.g. terminated, or an
                # unrelayed crash) and bail to the error path below.
                if not proc.is_alive():
                    break
                continue

            kind = item.get("type")
            if kind == "progress":
                progress = _as_float(item.get("progress"))
                if progress >= 1.0:
                    if not reached_full_progress:
                        reached_full_progress = True
                        yield {
                            "model": model_key,
                            "status": "finalizing",
                            "progress": 1.0,
                            "message": "Finalizing download…",
                        }
                    continue
                downloaded_bytes = _as_int(item.get("downloaded"))
                total_bytes = _as_int(item.get("total"))
                msg = (
                    f"{progress * 100:.0f}% "
                    f"({downloaded_bytes / 1e9:.1f} / {total_bytes / 1e9:.1f} GB)"
                )
                yield {
                    "model": model_key,
                    "status": "downloading",
                    "progress": progress,
                    "message": msg,
                }
            else:
                ok = item.get("type") == "ok"
                if not ok:
                    error_obj = item.get("message")
                    error_msg = error_obj if isinstance(error_obj, str) else "Download failed"
                break
    finally:
        # Cancel / normal teardown: terminate the child so its file lock is
        # released immediately — this is what lets a re-download start cleanly
        # instead of blocking on the previous transfer.
        stop_event.set()
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=5)
        proc.close()
        feeder.join(timeout=1)

    if not ok:
        yield {
            "model": model_key,
            "status": "error",
            "progress": 0,
            "message": error_msg or "Download failed",
        }
        return

    # Phase 3: verify the downloaded file
    yield {
        "model": model_key,
        "status": "verifying",
        "progress": 1.0,
        "message": "Verifying SHA256…",
    }
    local = await loop.run_in_executor(None, sha256_of, target)
    if local == expected:
        yield {
            "model": model_key,
            "status": "complete",
            "progress": 1.0,
            "message": "Downloaded and verified.",
        }
    else:
        yield {
            "model": model_key,
            "status": "error",
            "progress": 0,
            "message": f"SHA256 mismatch: expected {expected}, got {local}",
        }

