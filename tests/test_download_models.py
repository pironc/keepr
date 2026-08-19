"""Unit test for the model-download hashing helper.

The shared SHA-256 helper lives in ``src.download`` (imported by both the
standalone ``scripts/download_models.py`` CLI and the in-app
``/api/models/download`` route) — this is the only piece of the download
path with no network dependency, so it's the only part that belongs under
``--disable-socket`` (see pyproject.toml / CLAUDE.md; the network-touching
download code is deliberately exempt from the test suite).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from src.download import sha256_of


def test_sha256_of_matches_hashlib_on_known_bytes(tmp_path: Path) -> None:
    content = b"keepr integrity check fixture \x00\x01\xff" * 1000
    target = tmp_path / "fixture.bin"
    target.write_bytes(content)

    assert sha256_of(target) == hashlib.sha256(content).hexdigest()


def test_sha256_of_reads_in_chunks_not_all_at_once(tmp_path: Path) -> None:
    content = b"a" * 5000
    target = tmp_path / "fixture.bin"
    target.write_bytes(content)

    assert sha256_of(target, chunk_size=64) == hashlib.sha256(content).hexdigest()
