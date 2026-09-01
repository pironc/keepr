"""Model status, selection, and download endpoints."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.api.context import AppContext, get_context
from src.api.sse import format_event
from src.config import Settings, load_model_selection, save_model_selection
from src.download import (
    EMBEDDING_DEFAULT_MODEL,
    LLM_DEFAULT_MODEL,
    MODEL_CATALOG,
    MODEL_DEFS,
    download_model_with_progress,
    resolve_catalog_entry,
)
from src.gguf_meta import classify_gguf_type, gguf_embedding_dimension
from src.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/models", tags=["models"])


# ── Status cache ───────────────────────────────────────────────────────
# The settings menu re-queries /status on every dropdown open. That endpoint
# does a directory scan + a per-file GGUF metadata read for classification,
# which is cheap but not free once the models dir holds several GB of files.
# Since the models folder only changes when the user explicitly does
# something (download, delete, select, or adds/removes a file in Finder), we
# cache the response and only recompute when a cheap directory snapshot says
# the contents changed — making the repeated open-from-cache path a single
# ``stat`` on the dir instead of re-reading every GGUF header.
#
# The snapshot fingerprint is: dir mtime_ns + sorted (filename, mtime_ns,
# size) of every *.gguf. A Finder write/delete, a download that lands, or a
# delete that unlinks all bump that fingerprint, so the cache can't serve a
# stale list. No wall-clock TTL — freshness is driven by actual change.
class _ModelStatusCache:
    __slots__ = ("fingerprint", "payload")

    def __init__(self) -> None:
        self.fingerprint: object = None
        self.payload: dict[str, object] | None = None


_status_cache: _ModelStatusCache = _ModelStatusCache()


def _models_dir_snapshot(models_dir: Path) -> tuple[str, object]:
    """Cheap fingerprint of the models directory contents.

    Reads only directory entries' metadata (no GGUF *headers*), so it's O(n)
    metadata stats — cheap enough to run on every dropdown open. Includes the
    resolved directory path so caches for different models dirs never collide
    between tests or setups.
    """
    base = str(models_dir.resolve())
    try:
        dir_mtime_ns = models_dir.stat().st_mtime_ns
    except OSError:
        dir_mtime_ns = 0
    files: list[tuple[str, int, int]] = []
    if models_dir.is_dir():
        try:
            files = sorted(
                (p.name, p.stat().st_mtime_ns, p.stat().st_size)
                for p in models_dir.glob("*.gguf")
                if p.is_file()
            )
        except OSError:
            files = []
    return (base, (dir_mtime_ns, tuple(files)))


async def _build_model_status(settings: Settings) -> dict[str, object]:
    """Compute the full status payload from scratch (never cached)."""
    models_dir = settings.models_dir
    available = _available_models(models_dir)

    selection = load_model_selection(settings.model_selection_path)

    types: dict[str, str | None] = {
        filename: classify_gguf_type(models_dir / filename) for filename in available
    }

    def _present(name: str) -> str:
        return name if name and (models_dir / name).is_file() else ""

    active_llm = _present(selection.get("llm", settings.llm_model_path.name))
    active_embedding = _present(
        selection.get("embedding", settings.embedding_model_path.name)
    )

    models: list[dict[str, object]] = []
    for entry in MODEL_CATALOG:
        path = models_dir / entry.filename
        models.append(
            {
                "key": entry.role,
                "filename": entry.filename,
                "label": entry.label,
                "size_hint": entry.size_hint,
                "blurb": entry.blurb,
                "exists": path.is_file(),
                "repo_id": entry.repo_id,
            }
        )

    return {
        "llm_driver": settings.llm_driver,
        "embedder": settings.embedder,
        "models_dir": str(models_dir.resolve()),
        "active_llm": active_llm,
        "active_embedding": active_embedding,
        "available": available,
        "types": types,
        "models": models,
    }


def _invalidate_model_status() -> None:
    """Force the status cache to recompute on next read.

    Called from the select/delete/download paths — code that already altered
    the models dir or persisted selection and wants the menu to reflect it
    immediately, without waiting for the next snapshot to notice.
    """
    _status_cache.fingerprint = None


# ── In-flight download registry ───────────────────────────────────────
# The set of model filenames currently being downloaded **or waiting** on the
# shared download lock. Tracked separately from the on-disk snapshot because a
# queued transfer hasn't produced a file yet, so the directory fingerprint can't
# see it — but the settings menu must still gray out that model's download row
# (you can't meaningfully queue a file twice).
#
# Frontend row-locks (src/web/static/app.js's _downloadToasts) already cover the
# same-session leave/re-enter case; this is the durable, backend-authoritative
# source that survives a full frontend reload, and it's what the Settings panel
# uses to rebuild the lock from scratch.
_in_flight: set[str] = set()


def _download_target_filenames(
    keys: list[str], repo_id: str | None, filename: str | None
) -> list[str]:
    """Resolve which on-disk filenames a download request will produce.

    Returns the filenames matching ``keys``/``(repo_id, filename)`` so the
    in-flight registry and the status payload can name exactly what's queued or
    downloading. Unknown entries are skipped (``download_model_with_progress``
    yields an error event for those).
    """
    out: list[str] = []
    for key in keys:
        entry = resolve_catalog_entry(key, repo_id, filename)
        if entry is not None:
            out.append(entry.filename)
    return out



class DownloadRequest(BaseModel):
    model: str  # "llm" | "embedding" | "all"
    repo_id: str | None = None  # optional specific catalog entry
    filename: str | None = None  # optional specific catalog entry


class SelectRequest(BaseModel):
    role: str  # "llm" | "embedding"
    filename: str  # basename of a .gguf file in the models dir, or "" to clear


class DeleteRequest(BaseModel):
    filename: str  # basename of a .gguf file in the models dir


def _available_models(models_dir: Path) -> list[str]:
    """Sorted basenames of the *.gguf files present in the models directory."""
    if not models_dir.is_dir():
        return []
    return sorted(p.name for p in models_dir.glob("*.gguf") if p.is_file())


def request_self_quit(app: FastAPI | None = None) -> None:
    """Gracefully shut down the current backend process.

    Prefers flipping the registered uvicorn ``Server``'s ``should_exit`` flag
    (set on ``app.state.uvicorn_server`` by ``backend_main.py``) over sending
    ourselves SIGTERM: on Windows, ``os.kill(self_pid, SIGTERM)`` is an
    unconditional ``TerminateProcess``, not a catchable signal, so it would
    skip the FastAPI lifespan shutdown entirely. ``should_exit`` triggers that
    same graceful shutdown without depending on OS signal delivery, so it
    works the same on every platform. The SIGTERM fallback only applies when
    no ``Server`` is registered — the ``uvicorn --reload`` dev runner and the
    test client, neither of which go through ``backend_main.py``. Isolated
    here (rather than inlined in the route) so tests can monkeypatch it
    without terminating the test runner.
    """
    server = getattr(app.state, "uvicorn_server", None) if app is not None else None
    if server is not None:
        server.should_exit = True
        return
    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):  # pragma: no cover - defensive
        logger.warning("Could not send SIGTERM to self")


def _require_bare_filename(filename: str) -> None:
    """Reject a filename that isn't a bare basename (i.e. contains a path).

    Both ``/select`` and ``/delete`` take a filename that must resolve inside
    the models directory, never outside it.
    """
    if filename != Path(filename).name:
        raise HTTPException(
            status_code=400, detail="Filename must be a bare .gguf name, not a path."
        )


def _reveal_in_file_manager(path: Path) -> None:
    """Open a directory in the OS file manager (Finder / Explorer / …).

    Best-effort convenience: failures are logged, never raised — a desktop
    shell is not part of the answer pipeline. The path is always a server-side
    directory (the models dir), never user input, so there is nothing to
    sanitize here.
    """
    path = path.resolve()
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        elif sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except OSError as exc:
        logger.warning("Could not reveal %s in file manager: %s", path, exc)


@router.get("/status")
async def model_status(context: AppContext = Depends(get_context)) -> dict[str, object]:
    """Return driver configuration, available models, and per-model file status.

    Cached (see _ModelStatusCache) so repeated dropdown opens don't re-scan
    the models directory and re-read every GGUF header each time; recomputes
    only when the directory actually changed. ``select``/``delete``/``download``
    explicitly invalidate the cache so in-app changes reflect immediately.
    """
    settings = context.settings
    models_dir = settings.models_dir

    snapshot = _models_dir_snapshot(models_dir)
    if _status_cache.fingerprint == snapshot and _status_cache.payload is not None:
        payload = _status_cache.payload
    else:
        payload = await _build_model_status(settings)
        _status_cache.fingerprint = snapshot
        _status_cache.payload = payload

    # Merge the live in-flight set every call: it changes independent of the
    # on-disk cache fingerprint (a queued transfer writes no file yet), so it
    # can't live in the cached payload or a freshly opened Settings menu would
    # see a stale (empty) download list.
    payload = {**payload, "downloading": sorted(_in_flight)}
    return payload


@router.post("/select")
async def model_select(
    body: SelectRequest, context: AppContext = Depends(get_context)
) -> dict[str, object]:
    """Persist a model choice (by role) AND apply it to the running process.

    Unlike the old behavior (persist + require a restart), selection now live-
    swaps the in-memory model: the driver/embedder is repointed at the chosen
    file the moment ``/select`` returns, so the very next chat call lazily loads
    it (surfacing as the normal "generating"/"embedding" state) with no popup
    and no app restart.

    A filename must match a ``.gguf`` already present in the models directory,
    or be empty to clear the selection and fall back to the default model.

    The one exception is an embedding-model swap whose vector width differs from
    the running embedder's (the width every existing index was built with): the
    flat index can't hold mixed widths, so swapping silently would leave retrieval
    broken (it raises loudly, not corrupts). That swap is refused here with an
    ``incompatible_dimensions`` flag so the UI can explain — never a silent
    break. The LLLM swap has no such constraint.
    """
    if body.role not in ("llm", "embedding"):
        raise HTTPException(status_code=400, detail=f"Unknown role: {body.role!r}")

    filename = body.filename.strip()
    settings = context.settings
    selection = load_model_selection(settings.model_selection_path)

    if filename:
        # Bare basename only — never a path, so the menu can't point at a file
        # outside the models dir.
        _require_bare_filename(filename)
        if not filename.endswith(".gguf"):
            raise HTTPException(status_code=400, detail=f"Not a .gguf file: {filename!r}")
        candidate = settings.models_dir / filename
        if not candidate.is_file():
            raise HTTPException(
                status_code=400, detail=f"No such model in {settings.models_dir}: {filename!r}"
            )
        target = candidate
    else:
        # Clearing the choice falls back to that role's default model file.
        default_name = (
            LLM_DEFAULT_MODEL.filename
            if body.role == "llm"
            else EMBEDDING_DEFAULT_MODEL.filename
        )
        target = settings.models_dir / default_name

    # Guard the embedder swap against a vector-width change. The target width is
    # read from GGUF metadata (cheap, no model load); None means "unknown", and
    # we conservatively allow the swap (a later mismatch still fails loudly).
    if body.role == "embedding":
        target_dim = await asyncio.to_thread(gguf_embedding_dimension, target)
        if target_dim is not None and target_dim != context.embedder.dimensions:
            return {
                "ok": False,
                "role": "embedding",
                "filename": filename or None,
                "swapped": False,
                "incompatible_dimensions": True,
                "detail": (
                    f"{target.name} embeds at {target_dim} dimensions, but the existing "
                    f"search index was built at {context.embedder.dimensions}. Switching "
                    "embedding models to a different vector width invalidates every "
                    "conversation's index and would need re-ingestion first — not a "
                    "silent swap. Keep the current embedder, or restart after clearing "
                    "documents if you truly want to change width."
                ),
            }

    if body.role == "llm":
        await context.llm_driver.set_model_path(target)
    else:
        await context.embedder.set_model_path(target)

    if filename:
        selection[body.role] = filename
    else:
        selection.pop(body.role, None)

    save_model_selection(settings.model_selection_path, selection)
    _invalidate_model_status()
    return {"ok": True, "role": body.role, "filename": filename or None, "swapped": True, "restart_required": False}


@router.post("/delete")
async def model_delete(
    body: DeleteRequest, context: AppContext = Depends(get_context)
) -> dict[str, object]:
    """Delete an installed ``.gguf`` model (a default model or a user-dropped
    custom one).  If the deleted model is the currently-selected one, that
    selection is cleared so the menu never points at a file that no longer
    exists.
    """
    filename = body.filename.strip()
    if not filename:
        raise HTTPException(status_code=400, detail="Missing model filename.")
    # Bare basename only — never a path, so a delete can't reach outside the
    # models dir (the same guard the select endpoint applies).
    _require_bare_filename(filename)

    settings = context.settings
    selection = load_model_selection(settings.model_selection_path)

    # Serialize with downloads: a delete of the same target can't race a
    # concurrent download's file write.
    async with context.download_lock:
        if filename.endswith(".gguf"):
            target = settings.models_dir / filename
            if not target.is_file():
                raise HTTPException(status_code=404, detail=f"No such model: {filename!r}")
            target.unlink()
            for role in ("llm", "embedding"):
                if selection.get(role) == filename:
                    selection.pop(role)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown model: {filename!r}")

    save_model_selection(settings.model_selection_path, selection)
    _invalidate_model_status()
    return {"ok": True, "filename": filename}


@router.post("/quit")
async def model_quit(request: Request) -> dict[str, object]:
    """Gracefully shut the backend down once the response has been flushed.

    The caller persists the new model via ``/select`` first, then hits this
    so the change takes effect on next launch. The shutdown is scheduled as a
    background task with a short delay so the HTTP response body reaches the
    client before the process tears down.
    """
    # Small delay so the HTTP response body flushes to the client before the
    # process tears itself down.
    async def _delayed_quit() -> None:
        await asyncio.sleep(0.2)
        await asyncio.to_thread(request_self_quit, request.app)

    asyncio.get_running_loop().create_task(_delayed_quit())
    return {"ok": True}


@router.post("/open-folder")
async def open_models_folder(context: AppContext = Depends(get_context)) -> dict[str, object]:
    """Reveal the models directory in the OS file manager."""
    models_dir = context.settings.models_dir
    models_dir.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(_reveal_in_file_manager, models_dir)
    return {"ok": True, "models_dir": str(models_dir.resolve())}


@router.post("/download")
async def model_download(
    body: DownloadRequest, context: AppContext = Depends(get_context)
) -> StreamingResponse:
    """Stream model download progress as SSE events.

    ``body.model`` selects the role ("llm"/"embedding"/"all"); ``body.repo_id``
    + ``body.filename`` optionally select a specific catalog entry (a lighter/heavier
    option) when provided and valid for that role.
    """
    if body.model not in ("llm", "embedding", "all"):
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model key: {body.model!r}. Use 'llm', 'embedding', or 'all'.",
        )

    if body.model == "all":
        keys = list(MODEL_DEFS)
        repo_id = filename = None
    else:
        keys = [body.model]
        repo_id = body.repo_id
        filename = body.filename

    return StreamingResponse(
        _model_download_stream(
            keys, context.settings.models_dir, context.download_lock, repo_id, filename
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _model_download_stream(
    keys: list[str],
    models_dir: Path,
    download_lock: asyncio.Lock,
    repo_id: str | None = None,
    filename: str | None = None,
) -> AsyncIterator[str]:
    """Yield SSE download-progress events for ``keys``.

    The shared ``download_lock`` serializes transfers so only one model writes
    into huggingface_hub's cache at a time.  It is held for the whole stream
    (released when the generator finishes *or* is closed by a client cancel),
    so the next queued download proceeds as soon as the current one clears.
    Without this a concurrent request could race the cache and corrupt it.
    On completion, the status cache is invalidated so the settings menu
    immediately reflects the newly installed model.
    """
    targets = _download_target_filenames(keys, repo_id, filename)
    _in_flight.update(targets)
    try:
        async with download_lock:
            for key in keys:
                async for event in download_model_with_progress(
                    key, models_dir, repo_id, filename
                ):
                    fmt = format_event("model_download_status", event)
                    yield fmt
                    # On a terminal result (complete / already_exists / error),
                    # invalidate the status cache so the menu reflects the
                    # change without waiting for the next directory snapshot.
                    status = event.get("status")
                    if status in ("complete", "already_exists", "error"):
                        _invalidate_model_status()
    finally:
        _in_flight.difference_update(targets)
