"""One-time model download helper.

NOT part of the test suite — deliberately kept out of `tests/` so
`--disable-socket` (see pyproject.toml) never touches it. This is the one
legitimate place in the repo that's allowed to talk to the network.

Verify the exact repo IDs and filenames on huggingface.co before running —
quantization-file naming varies by publisher and changes over time, so the
constants below are a well-known starting point, not a guarantee. Pick the
exact file you want from each repo's file listing.

Integrity check: Hugging Face's git-lfs storage keys blobs by SHA256 (not
MD5) — `ModelInfo.siblings[].lfs.sha256`. We fetch that upstream digest and
compare it against a local file's SHA256 before deciding whether to
(re)download, so an interrupted rerun can tell "already have it" from
"corrupted, redo it" instead of guessing from file size alone.

Usage:
    pip install -e ".[models]"
    python scripts/download_models.py
"""

from __future__ import annotations

import hashlib
from pathlib import Path

MODELS_DIR = Path("data/models")

LLM_REPO_ID = "bartowski/Qwen_Qwen3-8B-GGUF"
LLM_FILENAME = "Qwen_Qwen3-8B-Q6_K.gguf"

EMBEDDING_REPO_ID = "nomic-ai/nomic-embed-text-v2-moe-GGUF"
EMBEDDING_FILENAME = "nomic-embed-text-v2-moe.Q8_0.gguf"


def _sha256_of(path: Path, chunk_size: int = 1 << 20) -> str:
    """Streamed so multi-GB GGUF files never sit fully in memory at once."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_sha256(repo_id: str, filename: str) -> str:
    """Authoritative upstream digest, straight from the Hub API's LFS metadata
    — not computed from a downloaded copy, so it's a real independent check.
    """
    from huggingface_hub import HfApi

    info = HfApi().model_info(repo_id, files_metadata=True)
    siblings = info.siblings or []
    sibling = next((s for s in siblings if s.rfilename == filename), None)
    if sibling is None or sibling.lfs is None:
        # Non-LFS or missing files have no sha256 to compare against; fail
        # loudly rather than silently skip the integrity check.
        raise RuntimeError(f"no LFS sha256 metadata for {filename!r} in {repo_id} — can't verify")
    return sibling.lfs.sha256


def main() -> None:
    from huggingface_hub import hf_hub_download

    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    for repo_id, filename in ((LLM_REPO_ID, LLM_FILENAME), (EMBEDDING_REPO_ID, EMBEDDING_FILENAME)):
        target = MODELS_DIR / filename
        expected = _expected_sha256(repo_id, filename)

        if target.exists():
            local = _sha256_of(target)
            if local == expected:
                print(
                    f"Already have {filename} ({target.stat().st_size / 1e9:.2f} GB) "
                    f"— sha256 verified against {repo_id}, skipping download."
                )
                continue
            print(
                f"{filename} exists but its sha256 doesn't match {repo_id} "
                f"(local {local} != expected {expected}) — re-downloading."
            )
        else:
            print(f"Downloading {filename} from {repo_id} (expected sha256 {expected}) ...")

        path = hf_hub_download(repo_id=repo_id, filename=filename, local_dir=MODELS_DIR)
        print(f"  -> {path}")

    print(
        "\nDone. Point LLM_MODEL_PATH / EMBEDDING_MODEL_PATH in .env at these files, "
        "and set LLM_DRIVER=llama_cpp / EMBEDDER=llama_cpp to use them."
    )


if __name__ == "__main__":
    main()
