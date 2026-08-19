"""Shared request-scoped application context, wired once at startup by the
FastAPI lifespan in app.py. Kept in its own module (rather than importing
`app` directly from route modules) to avoid any circular-import coupling
between routes and the app factory.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from fastapi import Request

from src.config import Settings
from src.db.pool import SQLiteConnectionPool
from src.db.repository import Repository
from src.embeddings.base import Embedder
from src.ingestion.pipeline import IngestionPipeline
from src.ingestion.worker import IngestionWorker
from src.llm.base import LLMDriver
from src.rag.engine import RagEngine
from src.rag.generation_worker import GenerationWorker
from src.rag.index_manager import IndexManager


@dataclass(slots=True)
class AppContext:
    settings: Settings
    repository: Repository
    embedder: Embedder
    llm_driver: LLMDriver
    index_manager: IndexManager
    pipeline: IngestionPipeline
    engine: RagEngine
    generation_worker: GenerationWorker
    ingestion_worker: IngestionWorker
    db_pool: SQLiteConnectionPool
    # Serializes model downloads: only one remote transfer may touch the shared
    # huggingface_hub cache at a time (two writers into the same cache dir
    # corrupts it).  A second download request waits on this lock server-side
    # rather than racing the first; each still gets its own SSE stream/toast.
    download_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def get_context(request: Request) -> AppContext:
    context: AppContext = request.app.state.context
    return context
