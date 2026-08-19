"""Tests for the lightweight GGUF metadata classifier.

The classifier decides whether a ``.gguf`` is a language (LLM) model or an
embedding model by looking for a pooling layer in the GGUF metadata — never
from the filename. We build tiny synthetic GGUF headers so no real (multi-GB)
models are needed.
"""

from __future__ import annotations

import struct
from pathlib import Path

from src.gguf_meta import (
    classify_gguf_type,
    gguf_architecture,
    read_gguf_metadata,
)

_STRING = 8
_ARRAY = 9


def _str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<Q", len(b)) + b


def _scalar(vtype: int, value: bytes) -> bytes:
    return struct.pack("<I", vtype) + value


def _build_gguf(path: Path, kv: list[tuple[str, int, bytes]]) -> Path:
    """Write a minimal valid GGUF header (magic + counts + KV items)."""
    blob = bytearray()
    blob += b"GGUF"
    blob += struct.pack("<I", 3)  # version
    blob += struct.pack("<Q", 0)  # tensor_count
    blob += struct.pack("<Q", len(kv))  # metadata_kv_count
    for key, vtype, value in kv:
        blob += _str(key)
        blob += struct.pack("<I", vtype)
        blob += value
    path.write_bytes(bytes(blob))
    return path


def _embedding_gguf(path: Path, arch: str = "nomic-bert-moe") -> Path:
    """A header whose model exposes a pooling layer -> embedding model."""
    return _build_gguf(
        path,
        [
            ("general.architecture", _STRING, _str(arch)),
            (f"{arch}.pooling_type", 5, struct.pack("<i", 1)),  # int32
        ],
    )


def _llm_gguf(path: Path, arch: str = "qwen3") -> Path:
    """A header with no pooling key -> text-generation (LLM)."""
    return _build_gguf(path, [("general.architecture", _STRING, _str(arch))])


def test_read_metadata_parses_strings_and_architecture(tmp_path: Path) -> None:
    p = _embedding_gguf(tmp_path / "m.gguf")
    assert read_gguf_metadata(p)["general.architecture"] == "nomic-bert-moe"


def test_read_metadata_rejects_non_gguf(tmp_path: Path) -> None:
    p = tmp_path / "junk.gguf"
    p.write_bytes(b"this is not gguf at all .........")
    try:
        read_gguf_metadata(p)
    except ValueError:
        return
    raise AssertionError("expected ValueError for non-GGUF file")


def test_classify_embedding_by_pooling_type(tmp_path: Path) -> None:
    p = _embedding_gguf(tmp_path / "embed.gguf")
    assert classify_gguf_type(p) == "embedding"


def test_classify_llm_when_no_pooling_type(tmp_path: Path) -> None:
    p = _llm_gguf(tmp_path / "chat.gguf")
    assert classify_gguf_type(p) == "llm"


def test_classification_is_name_independent(tmp_path: Path) -> None:
    # No model names are consulted: an architecture we've "never seen" is
    # classified purely by pooling presence, not by matching any catalog.
    p = _embedding_gguf(tmp_path / "future-embed-xyz.gguf", arch="brand-new-embed-arch")
    assert classify_gguf_type(p) == "embedding"
    p2 = _llm_gguf(tmp_path / "future-chat-xyz.gguf", arch="brand-new-chat-arch")
    assert classify_gguf_type(p2) == "llm"


def test_classify_missing_or_corrupt_is_unclassified(tmp_path: Path) -> None:
    assert classify_gguf_type(tmp_path / "missing.gguf") is None
    bad = tmp_path / "bad.gguf"
    bad.write_bytes(b"::not::a::gguf::header")
    assert classify_gguf_type(bad) is None


def test_gguf_architecture_blank_when_absent(tmp_path: Path) -> None:
    p = _build_gguf(tmp_path / "anon.gguf", [("some.key", _STRING, _str("x"))])
    assert gguf_architecture(read_gguf_metadata(p)) is None
