"""FastAPI application: conversation CRUD + the multiplexed SSE message endpoint."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from src.api.context import AppContext
from src.api.routes_conversations import router as conversations_router
from src.api.routes_messages import router as messages_router
from src.concurrency import LockedEmbedder, LockedLLMDriver
from src.config import Settings
from src.db.pool import SQLiteConnectionPool
from src.db.repository import Repository
from src.embeddings.factory import build_embedder
from src.ingestion.pipeline import IngestionPipeline
from src.llm.factory import build_llm_driver
from src.logger import get_logger
from src.rag.engine import RagEngine
from src.rag.generation_worker import GenerationWorker
from src.rag.index_manager import IndexManager

# Skip dotenv in the frozen (PyInstaller-bundled) app —
# env vars come from the Tauri wrapper, not a .env file on disk.
if not os.environ.get("KEEPR_FROZEN"):
    load_dotenv()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings.from_env()
    db_pool = SQLiteConnectionPool(settings.database_path)
    repository = Repository(db_pool)
    await repository.initialize()

    # Separate locks, not one shared lock.  Two llama.cpp Llama instances
    # sharing the same Metal/CUDA device will contend at the GPU level if
    # both use GPU layers — embedding defaults to CPU-only for exactly this
    # reason (see EMBEDDING_GPU_LAYERS defaulting to 0 in config.py), so
    # the two locks never gate the same physical resource.  The LLM driver
    # holds its lock across the entire generation stream; the embedder holds
    # its lock only for the duration of one batch embedding pass (~1-2 s on
    # CPU), and must not be blocked for the length of a full generation just
    # to pre-embed files for a different, queued conversation.
    embedder_lock = asyncio.Lock()
    llm_lock = asyncio.Lock()
    embedder = LockedEmbedder(build_embedder(settings), embedder_lock)
    llm_driver = LockedLLMDriver(build_llm_driver(settings), llm_lock)
    index_manager = IndexManager(settings.index_dir, settings.vector_index_backend)
    pipeline = IngestionPipeline(repository, embedder, settings)
    engine = RagEngine(
        repository=repository,
        embedder=embedder,
        top_k=settings.retrieval_top_k,
        min_similarity=settings.retrieval_min_similarity,
    )
    generation_worker = GenerationWorker(repository, engine, index_manager, llm_driver, pipeline, settings.upload_dir, embedder)
    # Before the app can serve a single request: a message stuck at
    # RETRIEVING/GENERATING means a worker was actively touching the model
    # when the process last stopped. Note `make run` uses --reload, so this
    # fires on every file save during development, not just a rare crash.
    await generation_worker.recover_from_crash()
    generation_worker.start()

    app.state.context = AppContext(
        settings=settings,
        repository=repository,
        embedder=embedder,
        llm_driver=llm_driver,
        index_manager=index_manager,
        pipeline=pipeline,
        engine=engine,
        generation_worker=generation_worker,
        db_pool=db_pool,
    )
    logger.info(
        "keepr started: backend=%s tier=%s llm_driver=%s embedder=%s index=%s",
        settings.backend,
        settings.memory_tier.name,
        settings.llm_driver,
        settings.embedder,
        settings.vector_index_backend,
    )
    try:
        yield
    finally:
        try:
            await generation_worker.stop()
        finally:
            try:
                await db_pool.close()
            finally:
                try:
                    aclose = getattr(llm_driver, "aclose", None)
                    if aclose is not None:
                        await aclose()
                finally:
                    aclose = getattr(embedder, "aclose", None)
                    if aclose is not None:
                        await aclose()


app = FastAPI(title="keepr", version="0.1.0", lifespan=lifespan)
app.include_router(conversations_router)
app.include_router(messages_router)
_web_dir = os.environ.get("KEEPR_WEB_DIR", "src/web")

app.mount("/static", StaticFiles(directory=os.path.join(_web_dir, "static")), name="static")


_INDEX_HTML = os.path.join(_web_dir, "templates", "index.html")

_INJECT_RELOAD = """\
<meta name="server-startup" content="__STARTUP__">
<script>
(function(){var s=document.querySelector('meta[name="server-startup"]').content;
setInterval(function(){fetch('/health').then(function(r){return r.json()}).then(
function(d){if(d.startup&&String(d.startup)!==s)location.reload()}).catch(
function(){})},800)})();
</script>
</html>"""


def _render_index() -> HTMLResponse:
    with open(_INDEX_HTML, encoding="utf-8") as f:
        html = f.read()
    html = html.replace("</html>", _INJECT_RELOAD.replace("__STARTUP__", str(_STARTUP)))
    return HTMLResponse(html)


@app.get("/")
async def index() -> HTMLResponse:
    return _render_index()


@app.get("/chat/{full_path:path}")
async def chat_route(full_path: str = "") -> HTMLResponse:
    """Any /chat or /chat/<id> serves the SPA — routing is client-side."""
    return _render_index()


_STARTUP = time.time()


@app.get("/health")
async def health() -> dict[str, str | float]:
    return {"status": "ok", "startup": _STARTUP}
