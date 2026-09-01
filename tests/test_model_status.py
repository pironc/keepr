"""Tests for /api/models/status: the status cache and the model catalog.

The status endpoint is deliberately cached (see the `_ModelStatusCache` in
src/api/routes_models.py) so the settings menu's per-open dropdown refresh is a
cheap directory ``stat`` rather than a re-read of every GGUF header. These tests
prove the cache is coherent: an unchanged directory returns consistent data, a
change on disk invalidates and is reflected on the next call, an in-app delete
invalidates immediately, and the catalog ships the expected set of light/heavy
download options per role.

These run under ``--disable-socket`` (see conftest.py): classification of a
real GGUF requires a valid header, so the tests use an empty or fake-less
models dir and assert on structure/cache behavior rather than on the contents
of a particular model file.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.api import routes_models as _rm
from src.api.routes_models import _download_target_filenames


def _status(client: TestClient) -> Any:
    resp = client.get("/api/models/status")
    assert resp.status_code == 200
    return resp.json()


def test_status_includes_downloading_field(client: TestClient) -> None:
    """/status always reports the live in-flight set (empty at rest), never
    serving a stale cached version of it."""
    assert _status(client)["downloading"] == []


def test_download_target_filenames_resolves_specific_and_defaults() -> None:
    """The in-flight registry names the exact files a request will produce,
    including both targets of an "all" download."""
    # A specific light LLM catalog entry.
    names = _download_target_filenames(["llm"], "lm-kit/qwen-3-1.7b-instruct-gguf", "Qwen3-1.7B-Q8_0.gguf")
    assert names == ["Qwen3-1.7B-Q8_0.gguf"]
    # The historical default for a role (repo_id=None) still resolves.
    assert _download_target_filenames(["embedding"], None, None) == ["nomic-embed-text-v2-moe.Q8_0.gguf"]
    # An "all" download covers both roles.
    all_names = _download_target_filenames(["llm", "embedding"], None, None)
    assert len(all_names) == 2
    # An unknown entry yields nothing (download_model_with_progress errors on it).
    assert _download_target_filenames(["llm"], "nope/nope", "missing.gguf") == []


def _bump_mtime(path: Path) -> None:
    """Force a later mtime so a snapshot fingerprint is guaranteed to change."""
    future = time.time() + 5
    os.utime(path, (future, future))


def test_status_returns_expected_shape(client: TestClient) -> None:
    data = _status(client)
    assert data["llm_driver"] == "mock"
    assert data["embedder"] == "mock"
    assert data["active_llm"] == ""
    assert data["active_embedding"] == ""
    assert data["available"] == []
    # Catalog is keyed so the frontend can group per role.
    roles = [m["key"] for m in data["models"]]
    assert "llm" in roles
    assert "embedding" in roles
    assert len(data["models"]) >= 6  # default + light + heavy per role


def test_catalog_entries_have_display_metadata(client: TestClient) -> None:
    """Every catalog entry must ship the label/size the settings menu shows."""
    data = _status(client)
    for m in data["models"]:
        assert m["filename"].endswith(".gguf")
        assert m["repo_id"]
        assert m["label"]
        assert m["size_hint"]
        assert "exists" in m


def test_cached_status_is_stable_across_calls(client: TestClient) -> None:
    """An unchanged models dir must return byte-identical status, so repeated
    dropdown opens from the cache never re-classify or flicker."""
    first = _status(client)
    second = _status(client)
    assert first == second


def test_folder_change_invalidates_cache(
    client: TestClient, tmp_path: Path
) -> None:
    """Adding then removing a file in the models dir must be observed by the
    next status call (the snapshot fingerprint changes)."""
    before = _status(client)
    assert before["available"] == []

    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    dummy = models_dir / "dummy.gguf"
    dummy.write_bytes(b"not a real gguf")  # unreadable header → type None
    # Bump mtime deliberately so the snapshot fingerprint changes even if
    # filesystem timestamps are coarse.
    _bump_mtime(dummy)

    after_add = _status(client)
    assert "dummy.gguf" in after_add["available"]
    assert after_add["types"]["dummy.gguf"] is None  # unclassifiable header

    dummy.unlink()
    after_remove = _status(client)
    assert after_remove["available"] == []


def test_delete_invalidates_status_immediately(
    client: TestClient, tmp_path: Path
) -> None:
    """Deleting a model via the API must invalidate the cache so the menu
    reflects the removal without waiting for the snapshot TTL (there is none —
    freshness is change-driven, but the delete path forces it regardless)."""
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    dummy = models_dir / "x.gguf"
    dummy.write_bytes(b"garbage")
    assert "x.gguf" in _status(client)["available"]

    resp = client.post("/api/models/delete", json={"filename": "x.gguf"})
    assert resp.status_code == 200
    assert "x.gguf" not in _status(client)["available"]


async def test_downloading_field_tracks_live_download(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A model in the middle of a (queued or active) transfer is tracked in
    ``_in_flight`` and surfaces in /status's ``downloading`` list, and clears
    the moment the transfer ends — so the settings menu can gray that model's
    download row for the whole duration (and after a full page reload, not just
    within one Settings session). The real ``download_model_with_progress``
    does SHA256/HTTP, so we stub it with a slow fake."""
    async def fake_download(
        model_key: str,
        models_dir: Path,
        repo_id: str | None = None,
        filename: str | None = None,
    ) -> Any:
        yield {"model": model_key, "status": "verifying", "progress": 0}
        await asyncio.sleep(1.0)  # long enough to observe the in-flight state
        yield {"model": model_key, "status": "complete", "progress": 1.0}

    monkeypatch.setattr(_rm, "download_model_with_progress", fake_download)

    # Drive the stream generator directly (the HTTP/TestClient route needs two
    # concurrent clients; starlette's portal doesn't co-schedule them well). The
    # registry mutation lives in the generator, so exercising it proves the same
    # behavior the /status endpoint reports.
    target = tmp_path / "models"
    target.mkdir(parents=True, exist_ok=True)
    lock = asyncio.Lock()
    agen = _rm._model_download_stream(["llm"], target, lock, None, None)

    # Pull the first event out from under the generator with a task so we can
    # check the registry while it's still open & transferring.
    task = asyncio.ensure_future(anext(agen))
    # Give the generator a chance to run up to its slow sleep; then the file
    # must be listed as in-flight.
    await asyncio.sleep(0.1)
    assert "Qwen_Qwen3-8B-Q6_K.gguf" in _rm._in_flight
    first = await task
    assert first

    # Drain to near-completion; once finished the set clears.
    async for _ in agen:
        pass
    assert _rm._in_flight == set()
