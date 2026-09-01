"""Lightweight GGUF metadata reading — no full model load.

The embeddings/LLM dropdowns in Settings need to know, per ``.gguf`` file,
whether it's an *embedding* model or a *language (chat/LLM)* model. That
distinction cannot come from the filename or from a hardcoded list of model
names (architectures like ``qwen3`` / ``nomic-bert-moe`` grow constantly).
Instead we look at the model's real embedded metadata.

The reliable, name-independent signal is a **pooling layer**: llama.cpp only
assigns a pooling configuration to models usable as embedders, exposed as a
``<architecture>.pooling_type`` key in the GGUF metadata. A model without one
is a text-generation (LLM) model.

This module reads only the small key/value header block that sits at the
start of a GGUF file (before the tensor data) — so classifying even a
multi-GB file costs a few KB of I/O, not a full load into memory.
"""

from __future__ import annotations

import struct
from pathlib import Path

_GGUF_MAGICS = (b"GGUF", b"fggu", b"FGGU")

# GGUF value type -> byte width. type 8 = string, type 9 = array.
_GGUF_TYPE_SIZE = {
    0: 1,  # uint8
    1: 1,  # int8
    2: 2,  # uint16
    3: 2,  # int16
    4: 4,  # uint32
    5: 4,  # int32
    6: 4,  # float32
    7: 1,  # bool
    8: 0,  # string
    9: 0,  # array
    10: 8,  # uint64
    11: 8,  # int64
    12: 8,  # float64
}
_TT_STRING = 8
_TT_ARRAY = 9


def read_gguf_metadata(path: Path) -> dict[str, str]:
    """Return the GGUF metadata key/value pairs as a ``{key: str_value}`` map.

    Values are decoded to their string form where cheap (strings are returned
    as-is; scalar values as their raw bytes hex; arrays collapsed to a single
    marker). Only the metadata section is read — tensor data is never touched.

    Raises ``ValueError`` if the file isn't a readable GGUF.
    """
    with Path(path).open("rb") as f:
        magic = f.read(4)
        if magic not in _GGUF_MAGICS:
            raise ValueError("not a GGUF file (bad magic)")
        f.read(4)  # version
        f.read(8)  # tensor_count
        n_kv = struct.unpack("<Q", f.read(8))[0]

        def _read_str() -> str:
            length = struct.unpack("<Q", f.read(8))[0]
            if length > 1 << 22:  # 4 MiB sanity cap; a stray length is garbage
                raise ValueError("implausible GGUF metadata string length")
            return f.read(length).decode("utf-8", "replace")

        out: dict[str, str] = {}
        for _ in range(n_kv):
            key = _read_str()
            vtype = struct.unpack("<I", f.read(4))[0]
            if vtype == _TT_STRING:
                out[key] = _read_str()
            elif vtype == _TT_ARRAY:
                elem_type = struct.unpack("<I", f.read(4))[0]
                count = struct.unpack("<Q", f.read(8))[0]
                elem_size = _GGUF_TYPE_SIZE.get(elem_type)
                if elem_type == _TT_STRING:
                    for _ in range(count):
                        _read_str()
                elif elem_size:
                    f.seek(count * elem_size, 1)
                else:  # unknown element type — skip conservatively
                    f.seek(count * 8, 1)
                out[key] = "<array>"
            else:
                size = _GGUF_TYPE_SIZE.get(vtype, 8)
                out[key] = f.read(size).hex()
    return out


def gguf_architecture(meta: dict[str, str]) -> str | None:
    """Return ``general.architecture`` (e.g. ``qwen3``, ``nomic-bert-moe``)."""
    arch = meta.get("general.architecture")
    return arch.strip() if arch and arch.strip() else None


def gguf_embedding_dimension(path: Path) -> int | None:
    """Return the embedding vector width of a GGUF model, or ``None`` if it
    can't be determined from the header.

    Reads ``<arch>.embedding_length`` from the metadata — the token-embedding
    width, which is the dimension of the vectors an embedding model emits and
    therefore the width every vector in the flat search index must share.
    Values in the GGUF header are stored as 32-bit little-endian unsigned
    integers (surfaced by :func:`read_gguf_metadata` as raw bytes hex such as
    ``00030000`` for 768); some writers only emit ``embedding_length`` for the
    base model's token embeddings, so callers treat ``None`` as "unknown —
    don't block on it" rather than assuming a particular width.

    This reads only the header, never the model weights, so it is cheap enough
    to call on model selection.
    """
    try:
        meta = read_gguf_metadata(path)
    except (OSError, ValueError, struct.error):
        return None
    arch = gguf_architecture(meta)
    if not arch:
        return None
    raw = meta.get(f"{arch}.embedding_length")
    if raw is None or len(raw) != 8:
        return None
    try:
        value = int.from_bytes(bytes.fromhex(raw), "little")
    except ValueError:
        return None
    return value


def classify_gguf_type(path: Path) -> str | None:
    """Classify a ``.gguf`` as ``"llm"`` or ``"embedding"`` from metadata alone.

    The rule is purely structural, never name-based:
    - a ``<arch>.pooling_type`` metadata key means the model exposes a pooling
      layer → ``"embedding"``;
    - a readable header without one is a text-generation model → ``"llm"``;
    - header unreadable / missing / corrupt → ``None`` (unclassified, and the
      caller should surface the model in both menus rather than guess).

    No model names are consulted anywhere — classification is purely
    structural, so this classifier is uniform for every model rather than
    depending on a name matching some fixed catalog.
    """
    try:
        meta = read_gguf_metadata(path)
    except (OSError, ValueError, struct.error):
        return None

    arch = gguf_architecture(meta)
    if not arch:
        return None
    if any(key.startswith(arch + ".") and "pooling" in key for key in meta):
        return "embedding"
    return "llm"
