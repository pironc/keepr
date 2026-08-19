"""Tests for model-path resolution and the settings-menu model selection.

Covers the two safety-relevant pieces of the `models/` folder feature:
(1) the precedence order that decides which GGUF actually gets loaded
(env var > settings-menu selection > default filename), and (2) the
`/api/models/select` endpoint's validation, which must never persist a
path that escapes the models directory.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.config import (
    _resolve_model_path,
    load_model_selection,
    save_model_selection,
)

# ── pure resolution / persistence helpers ───────────────────────────────


def test_resolve_model_path_env_wins_over_selection_and_default(tmp_path: Path) -> None:
    models_dir = tmp_path / "models"
    assert _resolve_model_path("/abs/custom.gguf", "selected.gguf", "default.gguf", models_dir) == (
        Path("/abs/custom.gguf")
    )


def test_resolve_model_path_selection_wins_over_default(tmp_path: Path) -> None:
    models_dir = tmp_path / "models"
    assert _resolve_model_path(None, "selected.gguf", "default.gguf", models_dir) == (
        models_dir / "selected.gguf"
    )
    # an empty env value is treated as "unset", not as an override
    assert _resolve_model_path("", "selected.gguf", "default.gguf", models_dir) == (
        models_dir / "selected.gguf"
    )


def test_resolve_model_path_default_when_nothing_set(tmp_path: Path) -> None:
    models_dir = tmp_path / "models"
    assert (
        _resolve_model_path(None, None, "default.gguf", models_dir) == models_dir / "default.gguf"
    )


def test_load_model_selection_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_model_selection(tmp_path / "does-not-exist.json") == {}


def test_load_model_selection_malformed_file_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("this is not json {", encoding="utf-8")
    assert load_model_selection(path) == {}


def test_save_and_load_model_selection_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "selection.json"
    save_model_selection(path, {"llm": "a.gguf", "embedding": "b.gguf"})
    assert load_model_selection(path) == {"llm": "a.gguf", "embedding": "b.gguf"}


def test_load_model_selection_drops_blank_values(tmp_path: Path) -> None:
    path = tmp_path / "selection.json"
    path.write_text('{"llm": "a.gguf", "embedding": "  "}', encoding="utf-8")
    assert load_model_selection(path) == {"llm": "a.gguf"}


# ── /api/models/status + /api/models/select ─────────────────────────────


def test_model_status_lists_only_gguf_files_in_models_dir(
    client: TestClient, tmp_path: Path
) -> None:
    (tmp_path / "models" / "foo.gguf").write_bytes(b"fake weights")
    (tmp_path / "models" / "notes.txt").write_text("not a model", encoding="utf-8")

    data = client.get("/api/models/status").json()

    assert data["available"] == ["foo.gguf"]
    assert data["models_dir"] == str((tmp_path / "models").resolve())
    # The default LLM file isn't on disk, so it must NOT be reported as active —
    # otherwise the menu would show it as both "selected" and "to download".
    assert data["active_llm"] == ""


def test_model_status_empty_active_when_no_models_present(
    client: TestClient, tmp_path: Path
) -> None:
    # No .gguf files in the models dir — neither the persisted selection nor
    # the configured default should be shown, since both would reference a
    # file that isn't there. A stale name must not leak into the Settings UI.
    client.post("/api/models/select", json={"role": "llm", "filename": "gone.gguf"})

    data = client.get("/api/models/status").json()

    assert data["available"] == []
    assert data["active_llm"] == ""
    assert data["active_embedding"] == ""


def test_model_status_classifies_catalog_entries_from_metadata(
    client: TestClient, tmp_path: Path
) -> None:
    # Classification is metadata-only, not catalog-name-based: a present file
    # with no pooling key is an "llm", one with a pooling key is "embedding",
    # and a missing file is unclassified (None) rather than guessed.
    models = tmp_path / "models"
    _write_gguf(models / "general_llm.gguf", [("general.architecture", 8, "qwen3")])
    _write_gguf(
        models / "general_emb.gguf",
        [
            ("general.architecture", 8, "nomic-bert-moe"),
            ("nomic-bert-moe.pooling_type", 5, struct.pack("<i", 1)),
        ],
    )

    data = client.get("/api/models/status").json()
    assert data["types"]["general_llm.gguf"] == "llm"
    assert data["types"]["general_emb.gguf"] == "embedding"


def _write_gguf(path: Path, kv: list[tuple[str, int, bytes | str]]) -> None:
    def s(x: str) -> bytes:
        b = x.encode("utf-8")
        return struct.pack("<Q", len(b)) + b

    blob = bytearray(b"GGUF")
    blob += struct.pack("<I", 3)  # version
    blob += struct.pack("<Q", 0)  # tensor_count
    blob += struct.pack("<Q", len(kv))  # metadata_kv_count
    for key, vtype, value in kv:
        blob += s(key)
        blob += struct.pack("<I", vtype)
        if isinstance(value, str):
            blob += s(value)
        else:
            blob += value
    path.write_bytes(bytes(blob))


def test_model_status_types_map_classifies_all_available(client: TestClient, tmp_path: Path) -> None:
    # A real (pooling) embedding model and a no-pooling LLM, plus a corrupt file
    # that must be left unclassified (None).
    models = tmp_path / "models"
    # architecture foo-bert + its pooling_type -> embedding
    _write_gguf(
        models / "embedx.gguf",
        [("general.architecture", 8, "foo-bert"), ("foo-bert.pooling_type", 5, struct.pack("<i", 1))],
    )
    # architecture qwen9, no pooling -> llm
    _write_gguf(models / "llmx.gguf", [("general.architecture", 8, "qwen9")])
    (models / "broken.gguf").write_text("not a gguf", encoding="utf-8")

    data = client.get("/api/models/status").json()
    types = data["types"]
    assert types["embedx.gguf"] == "embedding"
    assert types["llmx.gguf"] == "llm"
    assert types["broken.gguf"] is None


def test_model_select_persists_choice(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "models" / "foo.gguf").write_bytes(b"fake weights")

    resp = client.post("/api/models/select", json={"role": "llm", "filename": "foo.gguf"})

    assert resp.status_code == 200
    assert resp.json()["restart_required"] is True
    assert load_model_selection(tmp_path / "selection.json") == {"llm": "foo.gguf"}


def test_model_select_rejects_missing_file(client: TestClient) -> None:
    resp = client.post("/api/models/select", json={"role": "llm", "filename": "nope.gguf"})
    assert resp.status_code == 400


def test_model_select_rejects_path_traversal(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "evil.gguf").write_bytes(b"outside the models dir")

    resp = client.post("/api/models/select", json={"role": "llm", "filename": "../evil.gguf"})

    assert resp.status_code == 400
    assert load_model_selection(tmp_path / "selection.json") == {}


def test_model_select_rejects_non_gguf(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "models" / "foo.txt").write_text("not a model", encoding="utf-8")

    resp = client.post("/api/models/select", json={"role": "embedding", "filename": "foo.txt"})

    assert resp.status_code == 400


def test_model_delete_removes_file_and_clears_selection(
    client: TestClient, tmp_path: Path
) -> None:
    (tmp_path / "models" / "foo.gguf").write_bytes(b"fake weights")
    client.post("/api/models/select", json={"role": "llm", "filename": "foo.gguf"})

    resp = client.post("/api/models/delete", json={"filename": "foo.gguf"})

    assert resp.status_code == 200
    assert not (tmp_path / "models" / "foo.gguf").exists()
    # The deleted model was the active selection — it must be cleared too.
    assert load_model_selection(tmp_path / "selection.json") == {}


def test_model_delete_rejects_path_traversal(client: TestClient) -> None:
    resp = client.post("/api/models/delete", json={"filename": "../evil.gguf"})
    assert resp.status_code == 400


def test_model_delete_rejects_missing(client: TestClient) -> None:
    resp = client.post("/api/models/delete", json={"filename": "nope.gguf"})
    assert resp.status_code == 404


def test_model_status_reflects_selection_before_restart(
    client: TestClient, tmp_path: Path
) -> None:
    (tmp_path / "models" / "foo.gguf").write_bytes(b"fake weights")
    client.post("/api/models/select", json={"role": "llm", "filename": "foo.gguf"})

    data = client.get("/api/models/status").json()

    # The menu reports the persisted choice, not the (still-loaded) model, so
    # a selection never appears to revert when the menu is reopened.
    assert data["active_llm"] == "foo.gguf"


def test_model_quit_returns_ok(client: TestClient) -> None:
    resp = client.post("/api/models/quit")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_request_self_quit_sends_sigterm_to_self(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import signal

    import src.api.routes_models as rm

    killed: list[tuple[int, signal.Signals]] = []
    # Patch the global os module request_self_quit uses (it calls os.kill).
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

    rm.request_self_quit()

    assert len(killed) == 1
    assert killed[0][0] == os.getpid()
    assert killed[0][1] == signal.SIGTERM


def test_model_open_folder_reveals_models_dir(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.api.routes_models as rm

    revealed: list[Path] = []
    # Never shell out to Finder/Explorer in a test — record the target instead.
    monkeypatch.setattr(rm, "_reveal_in_file_manager", lambda p: revealed.append(p))

    models_dir = tmp_path / "models"
    models_dir.rmdir()  # prove the endpoint (re)creates it on demand

    resp = client.post("/api/models/open-folder")

    assert resp.status_code == 200
    assert resp.json()["models_dir"] == str(models_dir.resolve())
    assert models_dir.is_dir()
    assert len(revealed) == 1
    assert revealed[0].resolve() == models_dir.resolve()
