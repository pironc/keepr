"""Hardware detection, memory tiers, and path/hyperparameter configuration.

Growable by design: `MEMORY_TIERS` is a named, ordered list, not a single
hardcoded RAM budget. Running this on a laptop picks the "standard" tier;
running the identical code on a dedicated machine with more RAM picks
"large" or "server" automatically — a bigger model, a longer context window,
same `LLMDriver` interface. Adding a tier for an even bigger box is a
one-line addition, not a rewrite.
"""

from __future__ import annotations

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
    recommended_model: str
    context_window: int
    description: str


MEMORY_TIERS: tuple[MemoryTier, ...] = (
    MemoryTier(
        name="minimal",
        min_ram_gb=8.0,
        recommended_model="Qwen3 4B Instruct, Q4_K_M (~2.5GB)",
        context_window=4096,
        description="Tight budget: small model, short context.",
    ),
    MemoryTier(
        name="standard",
        min_ram_gb=16.0,
        recommended_model="Qwen3 8B, Q6_K (~6.7GB)",
        context_window=8192,
        description="The default target for this project (e.g. a 24GB MacBook Air).",
    ),
    MemoryTier(
        name="large",
        min_ram_gb=32.0,
        recommended_model="Qwen3 8B, Q8_0 (~8.7GB), or Qwen3 14B",
        context_window=16384,
        description="A dedicated workstation with headroom: higher quant or a bigger model, longer context.",
    ),
    MemoryTier(
        name="server",
        min_ram_gb=64.0,
        recommended_model="A 70B-class model (quantized), or multiple concurrent model instances",
        context_window=32768,
        description=(
            "A dedicated server/GPU box — the explicit scale-up path for this project: "
            "same LLMDriver interface, a bigger model behind it."
        ),
    ),
)


def select_memory_tier(available_ram_gb: float) -> MemoryTier:
    eligible = [tier for tier in MEMORY_TIERS if available_ram_gb >= tier.min_ram_gb]
    return max(eligible, key=lambda tier: tier.min_ram_gb) if eligible else MEMORY_TIERS[0]


@dataclass(slots=True)
class Settings:
    backend: Backend
    memory_tier: MemoryTier

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

        return cls(
            backend=backend,
            memory_tier=tier,
            llm_driver=os.environ.get("LLM_DRIVER", "mock"),
            llm_model_path=Path(
                os.environ.get("LLM_MODEL_PATH", "data/models/Qwen_Qwen3-8B-Q6_K.gguf")
            ),
            llm_context_window=int(os.environ.get("LLM_CONTEXT_WINDOW", str(tier.context_window))),
            llm_gpu_layers=int(os.environ.get("LLM_GPU_LAYERS", "-1")),
            embedder=os.environ.get("EMBEDDER", "mock"),
            embedding_model_path=Path(
                os.environ.get("EMBEDDING_MODEL_PATH", "data/models/nomic-embed-text-v2-moe.Q8_0.gguf")
            ),
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
