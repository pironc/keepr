"""Hardware detection, memory tiers, and path/hyperparameter configuration.

Growable by design: `MEMORY_TIERS` is a named, ordered list, not a single
hardcoded RAM budget. Running this on a laptop picks the "standard" tier;
running the identical code on a dedicated machine with more RAM picks
"large" or "server" automatically — a bigger model, a longer context window,
same `LLMDriver` interface. Adding a tier for an even bigger box is a
one-line addition, not a rewrite.
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import psutil

Backend = Literal["cuda", "metal", "cpu"]


def detect_backend() -> Backend:
    """Best-effort hardware detection with zero ML-framework dependency.

    llama-cpp-python decides which compiled backend (Metal/CUDA/CPU) it
    actually uses internally based on its own build flags — this function
    only produces a friendly diagnostic, not a value fed into inference.
    """
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return "metal"
    if shutil.which("nvidia-smi") is not None:
        try:
            subprocess.run(["nvidia-smi"], capture_output=True, check=True, timeout=2)
            return "cuda"
        except (OSError, subprocess.SubprocessError):
            pass
    return "cpu"


@dataclass(slots=True, frozen=True)
class MemoryTier:
    name: str
    min_ram_gb: float
    context_window: int


MEMORY_TIERS: tuple[MemoryTier, ...] = (
    MemoryTier(name="minimal", min_ram_gb=8.0, context_window=4096),
    MemoryTier(name="standard", min_ram_gb=16.0, context_window=8192),
    MemoryTier(name="large", min_ram_gb=32.0, context_window=16384),
    MemoryTier(name="server", min_ram_gb=64.0, context_window=32768),
)


def select_memory_tier(available_ram_gb: float) -> MemoryTier:
    eligible = [tier for tier in MEMORY_TIERS if available_ram_gb >= tier.min_ram_gb]
    return max(eligible, key=lambda tier: tier.min_ram_gb) if eligible else MEMORY_TIERS[0]


# Model weights live in a root `models/` directory, deliberately separate from
# `data/` (private runtime state). The defaults below are *filenames* resolved
# against that directory; a user can point LLM_MODEL_PATH / EMBEDDING_MODEL_PATH
# at any llama.cpp-compatible GGUF, or pick one in the settings menu, which
# persists the choice to MODEL_SELECTION_PATH and applies it on the next start.
DEFAULT_MODELS_DIR = Path("models")
DEFAULT_LLM_MODEL = "Qwen_Qwen3-8B-Q6_K.gguf"
DEFAULT_EMBEDDING_MODEL = "nomic-embed-text-v2-moe.Q8_0.gguf"
DEFAULT_MODEL_SELECTION_PATH = Path("data/model_selection.json")


def load_model_selection(path: Path) -> dict[str, str]:
    """Read a persisted model selection (filenames keyed by role: llm/embedding).

    Written by `POST /api/models/select` and read here at startup so a choice
    survives a restart. A missing or malformed file is treated as "no
    selection", never a crash — the defaults still apply.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, str) and v.strip()
    }


def save_model_selection(path: Path, selection: dict[str, str]) -> None:
    """Persist a model selection (atomic-enough for a single-user app)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(selection, indent=2), encoding="utf-8")
    tmp.replace(path)


def _resolve_model_path(
    env_value: str | None, selected: str | None, default_filename: str, models_dir: Path
) -> Path:
    """Precedence: explicit env var > settings-menu selection > default filename."""
    if env_value and env_value.strip():
        return Path(env_value.strip())
    if selected:
        return models_dir / selected
    return models_dir / default_filename


def _default_driver(model_path: Path) -> str:
    """Driver to use when LLM_DRIVER/EMBEDDER isn't set explicitly.

    Packaged app (`KEEPR_FROZEN` set by backend_main.py when running from the
    PyInstaller bundle — see src/api/app.py's matching check): always
    `llama_cpp`, even with no model downloaded yet or a package/load failure.
    An end user must see the real "no model installed" refusal
    (RagEngine.answer's availability gate / ModelUnavailableError) instead of
    silently getting a meaningless answer from the mock driver — mock is a
    dev/test convenience, never something a real user should hit unknowingly.

    Dev/CI (KEEPR_FROZEN unset): real inference only if a model file is
    actually there AND the optional `llama_cpp` package is actually
    importable (a dev venv from a plain `make install` doesn't have it — a
    `models/` directory left over from an earlier, differently-installed venv
    shouldn't crash the app trying to import a package that isn't there).
    Mock otherwise, for a friction-free default on a fresh checkout."""
    if os.environ.get("KEEPR_FROZEN"):
        return "llama_cpp"
    if not model_path.is_file():
        return "mock"
    return "llama_cpp" if importlib.util.find_spec("llama_cpp") is not None else "mock"


@dataclass(slots=True)
class Settings:
    backend: Backend
    memory_tier: MemoryTier

    models_dir: Path
    model_selection_path: Path

    llm_driver: str
    llm_model_path: Path
    llm_context_window: int
    llm_gpu_layers: int

    embedder: str
    embedding_model_path: Path
    embedding_gpu_layers: int

    vector_index_backend: str
    retrieval_top_k: int
    retrieval_min_similarity: float

    chunk_size: int
    chunk_overlap: int

    database_path: Path
    index_dir: Path
    upload_dir: Path

    @classmethod
    def from_env(cls) -> Settings:
        backend = detect_backend()

        override_tier_name = os.environ.get("MEMORY_TIER", "").strip()
        if override_tier_name:
            tier = next((t for t in MEMORY_TIERS if t.name == override_tier_name), None)
            if tier is None:
                valid = ", ".join(t.name for t in MEMORY_TIERS)
                raise ValueError(f"Unknown MEMORY_TIER {override_tier_name!r}; valid options: {valid}")
        else:
            tier = select_memory_tier(psutil.virtual_memory().total / (1024**3))

        models_dir = Path(os.environ.get("MODELS_DIR", str(DEFAULT_MODELS_DIR)))
        selection_path = Path(
            os.environ.get("MODEL_SELECTION_PATH", str(DEFAULT_MODEL_SELECTION_PATH))
        )
        selection = load_model_selection(selection_path)

        llm_model_path = _resolve_model_path(
            os.environ.get("LLM_MODEL_PATH"), selection.get("llm"), DEFAULT_LLM_MODEL, models_dir
        )
        embedding_model_path = _resolve_model_path(
            os.environ.get("EMBEDDING_MODEL_PATH"),
            selection.get("embedding"),
            DEFAULT_EMBEDDING_MODEL,
            models_dir,
        )

        return cls(
            backend=backend,
            memory_tier=tier,
            models_dir=models_dir,
            model_selection_path=selection_path,
            llm_driver=os.environ.get("LLM_DRIVER", "").strip() or _default_driver(llm_model_path),
            llm_model_path=llm_model_path,
            llm_context_window=int(os.environ.get("LLM_CONTEXT_WINDOW", str(tier.context_window))),
            llm_gpu_layers=int(os.environ.get("LLM_GPU_LAYERS", "-1")),
            embedder=os.environ.get("EMBEDDER", "").strip() or _default_driver(embedding_model_path),
            embedding_model_path=embedding_model_path,
            embedding_gpu_layers=int(os.environ.get("EMBEDDING_GPU_LAYERS", "0")),
            vector_index_backend=os.environ.get("VECTOR_INDEX_BACKEND", "flat"),
            retrieval_top_k=int(os.environ.get("RETRIEVAL_TOP_K", "5")),
            retrieval_min_similarity=float(os.environ.get("RETRIEVAL_MIN_SIMILARITY", "0.22")),
            chunk_size=int(os.environ.get("CHUNK_SIZE", "800")),
            chunk_overlap=int(os.environ.get("CHUNK_OVERLAP", "150")),
            database_path=Path(os.environ.get("DATABASE_PATH", "data/keepr.db")),
            index_dir=Path(os.environ.get("INDEX_DIR", "data/index")),
            upload_dir=Path(os.environ.get("UPLOAD_DIR", "data/uploads")),
        )
