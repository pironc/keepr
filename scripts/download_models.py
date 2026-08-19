"""One-time model download helper.

NOT part of the test suite — deliberately kept out of `tests/` so
`--disable-socket` (see pyproject.toml) never touches it. This is the one
legitimate place in the repo that's allowed to talk to the network.

INTEGRATION NOTE: the model catalog (repo IDs + filenames) and the hash/
verify helpers live in `src.download`. This script imports them rather than
duplicating them, so a quant bump or repo rename has exactly one home — the
Settings-menu downloader (`/api/models/download`) and this CLI can never
silently disagree about what to fetch. We only import the pure constants and
helpers here; all network I/O still happens in this script's `main()`, under
this module, so `--disable-socket` tests are unaffected.

Usage:
    python scripts/download_models.py
"""

from __future__ import annotations

from pathlib import Path

from src.download import (
    MODEL_DEFS,
    _hub_token,
    expected_sha256,
    sha256_of,
)

MODELS_DIR = Path("models")


def main() -> None:
    from huggingface_hub import hf_hub_download

    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    for repo_id, filename in MODEL_DEFS.values():
        target = MODELS_DIR / filename
        expected = expected_sha256(repo_id, filename)

        if target.exists():
            local = sha256_of(target)
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

        path = hf_hub_download(
            repo_id=repo_id, filename=filename, local_dir=MODELS_DIR, token=_hub_token()
        )
        print(f"  -> {path}")

    print(
        "\nDone. Point LLM_MODEL_PATH / EMBEDDING_MODEL_PATH in .env at these files, "
        "and set LLM_DRIVER=llama_cpp / EMBEDDER=llama_cpp to use them."
    )


if __name__ == "__main__":
    main()
