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
from src.config import load_model_selection, save_model_selection
from src.download import MODEL_DEFS, download_model_with_progress
from src.gguf_meta import classify_gguf_type
from src.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/models", tags=["models"])


class DownloadRequest(BaseModel):
    model: str  # "llm" | "embedding" | "all"


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
    """Return driver configuration, available models, and per-model file status."""
    settings = context.settings
    models_dir = settings.models_dir

    available = _available_models(models_dir)

    # The menu's checked state reflects the *persisted selection* (what will
    # load on the next restart), not the currently-loaded model — otherwise a
    # choice made in the menu would appear to "revert" when the menu is
    # reopened before a restart.  An absent file is surfaced as an empty active
    # name (see below), never as a stale filename whose file was deleted.
    selection = load_model_selection(settings.model_selection_path)

    # Classify every available file purely from its GGUF metadata (pooling-layer
    # presence) — no model names or catalog lookups are consulted, so third-party
    # and future models are handled uniformly. Unclassifiable files (broken/
    # unreadable header) are None, and the menu shows those in *both* dropdowns.
    types: dict[str, str | None] = {
        filename: classify_gguf_type(models_dir / filename) for filename in available
    }

    # "Active" means *downloaded and selected* — never just the configured
    # default name.  Reporting the default filename as active while its file is
    # absent made the Settings menu show a model as both "selected" and "to
    # download" (and the app as usable when it isn't).  An absent file yields an
    # empty active name, so the menu shows "Select a model..." and the download
    # row is the only signal for "not downloaded yet".
    def _present(name: str) -> str:
        return name if name and (models_dir / name).is_file() else ""

    active_llm = _present(selection.get("llm", settings.llm_model_path.name))
    active_embedding = _present(selection.get("embedding", settings.embedding_model_path.name))

    models: list[dict[str, object]] = []
    for key, (repo_id, filename) in MODEL_DEFS.items():
        path = models_dir / filename
        models.append(
            {
                "key": key,
                "filename": filename,
                "exists": path.is_file(),
                "repo_id": repo_id,
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


@router.post("/select")
async def model_select(
    body: SelectRequest, context: AppContext = Depends(get_context)
) -> dict[str, object]:
    """Persist a model choice (by role) — takes effect on the next restart.

    The filename must match a ``.gguf`` file already present in the models
    directory, or be empty to clear the selection and fall back to the default.
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
        selection[body.role] = filename
    else:
        selection.pop(body.role, None)

    save_model_selection(settings.model_selection_path, selection)
    return {"ok": True, "role": body.role, "filename": filename or None, "restart_required": True}


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
    """Stream model download progress as SSE events."""
    if body.model not in ("llm", "embedding", "all"):
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model key: {body.model!r}. Use 'llm', 'embedding', or 'all'.",
        )

    keys = list(MODEL_DEFS) if body.model == "all" else [body.model]

    return StreamingResponse(
        _model_download_stream(keys, context.settings.models_dir, context.download_lock),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _model_download_stream(
    keys: list[str], models_dir: Path, download_lock: asyncio.Lock
) -> AsyncIterator[str]:
    """Yield SSE download-progress events for ``keys``.

    The shared ``download_lock`` serializes transfers so only one model writes
    into huggingface_hub's cache at a time.  It is held for the whole stream
    (released when the generator finishes *or* is closed by a client cancel),
    so the next queued download proceeds as soon as the current one clears.
    Without this a concurrent request could race the cache and corrupt it.
    """
    async with download_lock:
        for key in keys:
            async for event in download_model_with_progress(key, models_dir):
                yield format_event("model_download_status", event)
