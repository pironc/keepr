# CLAUDE.md

Guidance for Claude Code (and future contributors) working in this repo.
See [README.md](README.md) for user-facing docs and
[ARCHITECTURE.md](ARCHITECTURE.md) for the full technical design and
scaling story — read that before changing anything in `src/vectorstore/`,
`src/llm/`, or `src/embeddings/`, since each has a deliberate rationale
that isn't obvious from the code alone.

## What this is

keepr is a privacy-first, local-first RAG chat app: drop in documents,
ask questions, get answers grounded only in what was uploaded, with
citations, entirely offline-capable. `LLM_DRIVER=mock` /
`EMBEDDER=mock` (the defaults) need zero model downloads; `llama_cpp`
drivers give real local inference via GGUF models.

## Commands

```bash
make install          # pip install -e ".[dev]"
make run              # uvicorn src.api.app:app --reload --port 8000
make download-models   # fetch GGUF models (the only network-touching step)
make test              # pytest
make lint               # ruff check .
make typecheck           # mypy (strict — see pyproject.toml)
make ci                   # lint + typecheck + test — run before considering anything done
make clean                # wipes DB/index/uploads (app state) — NOT model weights
make clean-models          # wipes data/models/ too — separate on purpose, see Makefile
```

Run a single test: `pytest tests/test_rag_engine.py::test_refuses_before_calling_the_llm_when_index_is_empty`.

`data/` is entirely generated/downloaded (gitignored) — nothing under it
is checked in, unlike a project with seed fixtures. Tests never touch it;
they use `tmp_path`-scoped SQLite/index files per test.

`src/db/schema.py` has no migrations mechanism — `CREATE TABLE IF NOT
EXISTS` is a no-op against an already-existing table, so a new column
(this has happened twice now: `content_hash` on `documents`, `status`/
`error_message` on `messages`) needs `make clean` against any existing
local `data/keepr.db` before the app will start again. This is an
accepted, deliberate tradeoff for a pre-v1 project with no production
data, not an oversight — don't add a migrations framework for it.

## Directory map

- `src/models.py` — every Pydantic v2 shape shared across the codebase.
  `SourceRef` (`PageRef | TimeRef`, discriminated on `kind`) is the detail
  that makes citations growable to audio/video later without touching
  `Chunk`/`Citation`/anything downstream.
- `src/config.py` — hardware detection (`detect_backend()`, no torch
  dependency) and `MEMORY_TIERS`, a named ladder (not a hardcoded
  budget) — see ARCHITECTURE.md's scaling section before changing this.
- `src/db/pool.py` — `SQLiteConnectionPool`: lazy connections, a partial
  failure during startup closes whatever was already opened (doesn't
  strand it), `close()` drains the pool before closing so an in-flight
  `acquire()` is never yanked out from under it. `src/db/repository.py`
  is the *only* module that speaks SQL — that's what makes a future
  Postgres swap contained to one file.
- `src/ingestion/base.py` — the `Ingestor` protocol. Every implementation
  reduces a file to `TextSegment`s; chunking/embedding/indexing/citation
  are uniform downstream of that. `AudioVideoIngestor` is a deliberate,
  clean stub (`UnsupportedSourceError`, not a crash or silent no-op) —
  implementing real transcription later means filling in `extract()`
  there, nothing else.
- `src/ingestion/pipeline.py` — `IngestionPipeline.ingest()` hashes the
  file content (SHA-256) and checks it against already-indexed documents
  in that conversation *before* doing any work — re-attaching a file
  that's already indexed is a no-op, not a duplicate. This was a real bug
  (re-dropping a file doubled every one of its chunks in the index) caught
  by comparing a live run's output against expected behavior; keep the
  regression tests in `tests/test_ingestion_pipeline.py` if you touch this.
- `src/vectorstore/` — `VectorIndex` protocol, `NumpyFlatIndex` (float32,
  exact) and `QuantizedNumpyFlatIndex` (hand-rolled int8 scalar
  quantization — not a library call). Both are deliberately hand-rolled;
  read ARCHITECTURE.md before reaching for Chroma/FAISS/Qdrant here, the
  reasoning is specific, not a style preference.
- `src/llm/` — `LLMDriver` ABC (streams tokens via an async generator),
  `MockLLMDriver` (deterministic, parses `[chunk_N]` tags out of the
  system prompt, scoped to the `<context>` block only — not the whole
  prompt, which also contains an example `[chunk_3]` citation in its own
  instructions — no model needed) and `LlamaCppDriver` (bridges
  llama-cpp-python's *synchronous* streaming generator into an async one
  via a background thread + `asyncio.Queue`; don't "simplify" this into
  a plain `async def` wrapper, it would block the event loop per token).
  Confirmed live with Qwen3-8B: reasoning models served this way default
  to opening every response with a real, literal `<think>...</think>`
  block of chain-of-thought — actual generated text, not a template
  artifact — so `_strip_thinking` filters it out of the token stream
  itself (buffers only until the open/close tag is unambiguously resolved,
  handling a tag split across several streamed chunks); tested directly
  in `tests/test_llama_cpp_driver.py` without needing a real model.
- `src/rag/prompts.py` — rule 6 pushes the model toward a short flowing
  paragraph over a bulleted/numbered list by default (confirmed live: a
  weaker first version of this rule was ignored — Qwen3-8B's own bias
  toward structured output for multi-point answers is strong enough that
  the rule needs to be explicit and give a worked example, not just say
  "be concise").
- `src/rag/engine.py` — the actual anti-hallucination mechanism: refuses
  based on a retrieval-confidence **threshold check, before calling the
  LLM at all** — not a prompt asking the model to decide. Citation
  verification afterward is a set-membership check against the chunk IDs
  retrieved that turn. Both are tested (`tests/test_rag_engine.py`)
  including a driver that deliberately fabricates a citation, to prove
  the filter actually catches it. `answer()`'s `message_id` parameter is
  optional and defaults to `None` — only `GenerationWorker` ever passes
  one; every direct test call in `test_rag_engine.py` omits it and
  exercises the exact pre-`GenerationWorker` code path unchanged.
- `src/rag/generation_worker.py` — `GenerationWorker` runs message
  generation as a background task independent of any HTTP connection, so
  a page refresh mid-answer can't lose it (confirmed to, before this: the
  DB had a real question sent twice with zero assistant rows for either
  attempt). One durable, DB-ordered queue (`get_oldest_queued_message`,
  never trust `asyncio` scheduling order for "what's next"); one shared
  "current session" slot + `asyncio.Condition` for live-watching instead
  of a per-subscriber queue registry, since only one generation is ever
  active app-wide. `recover_from_crash()` runs at startup — see
  ARCHITECTURE.md's ["Resilient, queued message
  generation"](ARCHITECTURE.md#resilient-queued-message-generation)
  before touching this file.
- `src/concurrency.py` — `LockedEmbedder`/`LockedLLMDriver` wrap the real
  embedder/driver behind one shared `asyncio.Lock`, constructed once at
  startup. Fixes a real pre-existing race confirmed by reading the
  installed `llama_cpp` source directly: `Llama` has no internal locking,
  so ingestion's embedding calls and a query's embedding call could
  already corrupt shared model state if they landed concurrently.
- `src/api/` — FastAPI. `context.py` holds `AppContext` + the
  `get_context` DI function (kept separate from `app.py` to avoid
  circular imports between routes and the app factory).
  `routes_messages.py`'s `POST .../messages` enqueues onto
  `GenerationWorker` and streams `context.generation_worker.watch(...)`
  rather than driving `RagEngine.answer()` directly — the SSE payload
  (`document_status`, `message_status`, `token`, `citations`, `done`) is
  unchanged for a normal send. `GET .../messages/{id}/stream` is the
  reconnect route a refreshed page calls to reattach to an in-progress
  (or already-finished) generation.
- `src/web/` — plain HTML/CSS/vanilla JS, **no external JS dependency,
  no CDN script tag**. This was a deliberate choice, not an oversight:
  loading htmx/Alpine from a CDN would silently break the air-gapped
  claim on first page load. If you're tempted to add a frontend
  dependency, vendor it locally or don't. The same rule applies to the
  "Cream & Ember" visual design (`app.css`): its three type families
  (Bricolage Grotesque, Geist, JetBrains Mono — Google Fonts stand-ins
  for the source system's proprietary fonts) are vendored as local
  `.woff2` files under `src/web/static/fonts/`, not linked from Google
  Fonts, and every icon (attach, send, remove-chip) is a hand-authored
  inline `<svg>` rather than a Lucide/icon-font CDN include.

## Conventions

- **mypy --strict everywhere.** `make typecheck` must pass. llama-cpp-python
  ships real type stubs (unlike most ML libraries) — don't blanket-ignore
  its types; cast to `Any` only at the specific boundary where its
  TypedDict message unions are more granular than needed
  (see `src/llm/llama_cpp_driver.py`).
- **Pydantic v2** for cross-module data shapes; plain `dataclass` for
  internal, non-serialized wiring (`AppContext`, `Settings`, `MemoryTier`,
  the `TokenEvent`/`DoneEvent` RAG-engine events).
- **Never block the event loop.** Sync file I/O goes through
  `asyncio.to_thread` (ingestors, `IndexManager`); llama-cpp-python's
  blocking generators go through the background-thread + queue bridge
  pattern in `llama_cpp_driver.py`, not a naive async wrapper.
- **New Ingestor checklist:** implement `supports()` + `async extract()`
  returning `list[TextSegment]`, register it in
  `src/ingestion/registry.py`. Raise `UnsupportedSourceError` (not a bare
  exception) for a recognized-but-not-implemented type.
- **New VectorIndex checklist:** implement `add`/`search`/`save`/`__len__`
  matching `src/vectorstore/base.py`, wire it into
  `src/vectorstore/factory.py`'s `new_index`/`load_index`.

## Testing

- `asyncio_mode = "auto"` — `async def test_...` just works.
- `--disable-socket --allow-unix-socket` is set globally in
  `pyproject.toml` — any test that opens a real network socket fails the
  whole suite. `tests/test_airgapped.py` is the explicit positive control
  proving that block is live. The one script exempt from it,
  `scripts/download_models.py`, is deliberately kept outside `tests/`.
- Fixtures in `tests/conftest.py`: `db_pool`/`repository` (fresh
  `tmp_path`-scoped SQLite per test), `embedder` (`MockEmbedder` — a
  hashing-trick bag-of-words vectorizer, deterministic, preserves
  word-overlap similarity for meaningful retrieval tests), `llm_driver`
  (`MockLLMDriver`).
- `tests/test_vectorstore.py`'s quantization tests assert *measured*
  numbers (memory ratio, top-1 agreement) empirically calibrated against
  this repo's own fixtures — if you change the quantization scheme, rerun
  and recalibrate the thresholds, don't just loosen them until green.
- `tests/test_db_pool.py`, `tests/test_rag_engine.py`,
  `tests/test_generation_worker.py`, and `tests/test_llama_cpp_driver.py`
  each include at least one test that fails if the property being
  protected is removed (partial-connection-leak rollback; citation
  fabrication; abandoning a watch mid-generation still reaching `DONE`
  with real content; a `<think>` tag split across several streamed
  chunks) — that's intentional; keep that pattern for new safety-critical
  logic rather than testing only the happy path.
