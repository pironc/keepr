"""Unit tests for the live model-swap primitives that selection rides on.

``set_model_path`` on the concrete llama.cpp driver/embedder is what turns a
persisted selection into an in-process swap (repoint + unload; the next call
lazily loads the new file). ``gguf_embedding_dimension`` powers the embedder
width guard. These test the pieces directly without touching the filesystem
beyond a temp models dir.
"""

from __future__ import annotations

import struct
from pathlib import Path

from src.embeddings.llama_cpp_embedder import LlamaCppEmbedder
from src.gguf_meta import gguf_embedding_dimension
from src.llm.llama_cpp_driver import LlamaCppDriver


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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(blob))


def test_gguf_embedding_dimension_reads_from_header(tmp_path: Path) -> None:
    _write_gguf(
        tmp_path / "emb.gguf",
        [
            ("general.architecture", 8, "foo-bert"),
            ("foo-bert.embedding_length", 4, struct.pack("<I", 768)),
        ],
    )
    assert gguf_embedding_dimension(tmp_path / "emb.gguf") == 768


def test_gguf_embedding_dimension_unknown_for_bad_file(tmp_path: Path) -> None:
    (tmp_path / "bad.gguf").write_bytes(b"not a gguf")
    assert gguf_embedding_dimension(tmp_path / "bad.gguf") is None


def test_driver_set_model_path_repoints_and_unloads(tmp_path: Path) -> None:
    a = tmp_path / "a.gguf"
    b = tmp_path / "b.gguf"
    a.write_bytes(b"x")
    b.write_bytes(b"y")
    driver = LlamaCppDriver(model_path=a, n_ctx=1024)
    closed: list[object] = []

    class _FakeModel:
        def close(self) -> None:
            closed.append(self)

    fake = _FakeModel()
    driver._model = fake  # a loaded model to prove unload frees it

    driver.set_model_path(b)

    assert driver.model_path() == b
    assert closed == [fake]  # the previously-loaded model was closed
    assert driver._model is None

def test_driver_set_model_path_safe_when_nothing_loaded(tmp_path: Path) -> None:
    driver = LlamaCppDriver(model_path=tmp_path / "a.gguf", n_ctx=1024)

    driver.set_model_path(tmp_path / "b.gguf")

    assert driver.model_path() == tmp_path / "b.gguf"
    assert driver._model is None

def test_embedder_set_model_path_refreshes_dimensions(tmp_path: Path) -> None:
    _write_gguf(
        tmp_path / "emb.gguf",
        [
            ("general.architecture", 8, "foo-bert"),
            ("foo-bert.embedding_length", 4, struct.pack("<I", 768)),
        ],
    )
    embedder = LlamaCppEmbedder(model_path=tmp_path / "emb.gguf")
    closed: list[object] = []

    class _FakeModel:
        def close(self) -> None:
            closed.append(self)

    embedder._model = _FakeModel()

    _write_gguf(
        tmp_path / "other.gguf",
        [
            ("general.architecture", 8, "other-bert"),
            ("other-bert.embedding_length", 4, struct.pack("<I", 1024)),
        ],
    )
    embedder.set_model_path(tmp_path / "other.gguf")

    assert embedder.model_path() == tmp_path / "other.gguf"
    assert len(closed) == 1
    assert embedder._model is None
    assert embedder.dimensions == 1024
