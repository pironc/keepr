"""Shared request-scoped application context, wired once at startup by the
FastAPI lifespan in app.py. Kept in its own module (rather than importing
`app` directly from route modules) to avoid any circular-import coupling
between routes and the app factory.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from src.config import Settings
from src.db.pool import SQLiteConnectionPool
from src.db.repository import Repository
from src.embeddings.base import Embedder
from src.ingestion.pipeline import IngestionPipeline
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
    db_pool: SQLiteConnectionPool


def get_context(request: Request) -> AppContext:
    context: AppContext = request.app.state.context
    return context
