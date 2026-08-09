"""Unit test for the model-download script's pure hashing helper.

Only `_sha256_of` is tested here — it's the one piece of
`scripts/download_models.py` with no network dependency, so it's the only
part that belongs under `--disable-socket` (see pyproject.toml / CLAUDE.md;
the script itself is deliberately exempt from the test suite).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from scripts.download_models import _sha256_of


def test_sha256_of_matches_hashlib_on_known_bytes(tmp_path: Path) -> None:
    content = b"keepr integrity check fixture \x00\x01\xff" * 1000
    target = tmp_path / "fixture.bin"
    target.write_bytes(content)

    assert _sha256_of(target) == hashlib.sha256(content).hexdigest()


def test_sha256_of_reads_in_chunks_not_all_at_once(tmp_path: Path) -> None:
    content = b"a" * 5000
    target = tmp_path / "fixture.bin"
    target.write_bytes(content)

    assert _sha256_of(target, chunk_size=64) == hashlib.sha256(content).hexdigest()
