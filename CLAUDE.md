# CLAUDE.md

Guidance for Claude Code (and future contributors) working in this repo.
See [README.md](README.md) for user-facing docs and
[ARCHITECTURE.md](ARCHITECTURE.md) for the full technical design and
scaling story — read that before changing anything in `src/vectorstore/`,
`src/llm/`, or `src/embeddings/`, since each has a deliberate rationale
that isn't obvious from the code alone.

## Rule #1: the front-end is a representation, never a dependency

All real computation — document ingestion (extract/chunk/embed/index) and
message generation (retrieval + LLM inference) — runs entirely in the
backend as durable, DB-driven background workers (`IngestionWorker`,
`GenerationWorker`) whose lifetime is independent of any HTTP connection.
The frontend (`src/web/`) only ever watches and displays that state; it
never drives it, blocks it, or is required for it to make progress.
Closing a tab, losing network, or navigating away must never pause,
cancel, or lose work already handed to the backend. This is the same
guarantee "Resilient, queued message generation" (ARCHITECTURE.md)
already gives LLM generation — it applies identically to ingestion, and
to any future computational feature: if a feature's "is this done yet"
state can only be observed by keeping one specific browser tab or
connection open, that's a bug, not a UI detail to work around.

## What this is

keepr is a privacy-first, local-first RAG chat app: drop in documents,
ask questions, get answers grounded only in what was uploaded, with
citations, entirely offline-capable. `LLM_DRIVER`/`EMBEDDER` default to
`llama_cpp` the moment a real GGUF is actually present at the resolved
model path (env var > Settings-menu selection > default filename) and to
`mock` otherwise (`src/config.py`'s `_default_driver`) — mock needs zero
model downloads for a fresh checkout, and the packaged desktop app has no
shell to set an env var in, so a model downloaded/selected via Settings
just works with no separate driver toggle. An explicit `LLM_DRIVER`/
`EMBEDDER` env var always overrides this auto-detection either way.

"Offline-capable" describes what a user can choose, not a constraint the
app enforces on itself: `huggingface_hub` is a base dependency (not an
opt-in extra), so the Settings-menu downloader and
`scripts/download_models.py` are always available, no separate install
step needed. A user who wants to stay fully offline drops a GGUF into
`models/` directly instead — that path stays equally available either
way. Don't re-gate `huggingface_hub` behind an optional extra to chase a
stricter reading of "offline by default"; that tradeoff was deliberately
rejected.

## Commands

```bash
make install          # pip install -e ".[dev]"
make run              # uvicorn src.api.app:app --reload --port 8000
make test              # pytest
make lint               # ruff check .
make typecheck           # mypy (strict — see pyproject.toml)
make ci                   # lint + typecheck + test — run before considering anything done
make clean                # wipes DB/index/uploads (app state) — NOT model weights
# macOS app release: a push to `master` -> `.github/workflows/release.yml` builds the
# desktop app on a 4-leg matrix (macOS aarch64 + x86_64, Windows x64, Linux x64) via
# a `build` (matrix) job that uploads normalized artifacts, then a `publish` job merges
# them into the perpetual `latest` GitHub Release (a merged job avoids concurrent legs
# racing on the same release). Stable asset URLs, version-independent:
#   .../releases/latest/download/keepr-mac-{aarch64,x86_64}.dmg
#   .../releases/latest/download/keepr-windows-x86_64{-setup.exe,.msi}
#   .../releases/latest/download/keepr-linux-x86_64.{AppImage,deb}
# The release's display name is an auto-incrementing X.Y.Z (e.g. "Keepr v1.0.1"),
# computed fresh each run from the PREVIOUS run's name (no version stored in the
# repo) — see the `publish` job's "Compute next version name" step. Default is
# a patch bump; a `[minor]` marker anywhere in the triggering commit's message
# bumps the minor number instead (and resets patch) — `git config alias.minor
# '!git commit --allow-empty -m "[minor] $(git log -1 --pretty=%s)" && git push'`
# gives you `git minor` as a one-word way to push a real minor bump instead
# of the default hotfix, with the marker commit itself readable (its message
# is "[minor] <subject of the last real commit>", not a bare "[minor]").
# The README platform button row (macOS / Windows / Linux) points at the release page
# for the user to pick their arch. Docker image publishes only on `v*` tags.
# ARM Windows / ARM Linux are NOT built (PyInstaller-on-ARM is currently impractical
# for the Python backend); signed/notarized packages are a future follow-up.
# `platforms`: `src-tauri/src/main.rs` finds the bundled backend as
# `binaries/keepr-backend-<target-triple>[.exe]`, deriving the triple from
# the `KEEPR_TARGET_TRIPLE` env var emitted by `build.rs` — not a hardcoded
# per-OS list. Keep that derivation (and the cfg-branched `show_alert`: mac
# `osascript` / windows `MessageBoxW` / linux `zenity|kdialog|xmessage`)
# in sync if the matrix or binary naming ever changes.
# `tauri.conf.json`'s `app.windows[0].dragDropEnabled: false` is required, not
# cosmetic: Tauri's webview intercepts native OS file drops by default
# (`drag_drop_handler_enabled: true` in tauri-runtime, unconditional — no
# platform cfg guard), which swallows the drop before it ever reaches the
# page's own HTML5 dragover/drop listeners (src/web/static/app.js). Without
# this, drag-and-drop silently works in a browser (`make run`) but not in
# the Tauri window — don't remove it while chasing an unrelated
# drag-and-drop bug.
make clean-models          # wipes models/ too — separate on purpose, see Makefile
make wipe                    # full reset to a just-cloned state (venv, caches, build
                              # output, src-tauri/target, etc.) — models/ still exempt
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
  `load_model_selection`/`save_model_selection` persist the active
  LLM/embedding filenames to `data/model_selection.json`, applied on the
  next process start.
- `src/gguf_meta.py`, `src/model_unavailable.py`, `src/download.py`,
  `src/api/routes_models.py` — the Settings-menu model lifecycle.
  `gguf_meta.py` classifies a `.gguf` as `"llm"` or `"embedding"` by
  reading its metadata header (a `<arch>.pooling_type` key means
  embedding), never by filename, so the classifier doesn't need updating
  as new architectures show up. `model_unavailable.py`'s
  `ModelUnavailableError` is what the llama.cpp driver/embedder raise for
  a missing-or-broken model file, caught by the ingestion pipeline and RAG
  engine specifically so it becomes a clean document/message error instead
  of a raw llama-cpp exception crashing the SSE stream. `download.py`
  holds the model catalog and download/verify helpers shared between
  `scripts/download_models.py` and `routes_models.py`'s `/api/models/*`
  routes, so the CLI and in-app downloader can't disagree about what to
  fetch. Switching the active model (`/api/models/select`) persists the
  choice and then quits the whole app — the new model only actually loads
  on the next launch, there's no in-process reload. `/api/models/quit`'s
  `request_self_quit()` sends ourselves SIGTERM so uvicorn's normal
  shutdown path runs first (stops `GenerationWorker`, closes the DB pool,
  `aclose`s the driver/embedder), and app.js's `_quitAppNow()` then tells
  the Tauri shell to exit once the backend has confirmed it's down.
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
- `src/ingestion/pipeline.py` — `IngestionPipeline` splits the fast,
  connection-safe part from the slow, worker-owned part.
  `create_stub()` hashes the file content (SHA-256) and checks it against
  already-indexed *or already-in-progress* documents in that conversation
  before creating anything — re-attaching a file that's already indexed
  (or mid-pipeline from an earlier interrupted attempt) is a no-op, not a
  duplicate: without this check, re-dropping a file would double every one
  of its chunks in the index. `process_existing()` is the actual extract->
  chunk->embed->index state machine, called exclusively by `IngestionWorker` —
  keep the regression tests in `tests/test_ingestion_pipeline.py` if you
  touch either method.
- `src/ingestion/worker.py` — `IngestionWorker` runs ingestion as a
  background task independent of any HTTP connection, on its own queue,
  entirely separate from `GenerationWorker`'s — see Rule #1 above. This is
  what makes an embedding wait only for a prior embedding, never for an
  unrelated LLM generation, and what makes a document dropped into a chat
  that's then abandoned mid-embed still finish indexing on its own. Mirrors
  `GenerationWorker`'s shape closely (`recover_from_crash()`, poll loop,
  "nothing may ever escape this loop"). Its idle-unload of the embedder
  deliberately checks more than its own idleness — it defers unloading
  while ANY message anywhere is still non-terminal: without this, a
  document embedded early in a still-running generation would sit idle
  long enough to unload, then reload the moment the *next* document showed
  up mid-generation, for zero real RAM benefit over one generation's span
  (`tests/test_ingestion_worker.py`).
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
  Reasoning models served this way (e.g. Qwen3-8B) default to opening
  every response with a real, literal `<think>...</think>` block of
  chain-of-thought — actual generated text, not a template artifact — so
  `_strip_thinking` filters it out of the token stream itself (buffers
  only until the open/close tag is unambiguously resolved, handling a tag
  split across several streamed chunks); tested directly in
  `tests/test_llama_cpp_driver.py` without needing a real model.
- `src/rag/prompts.py` — rule 6 pushes the model toward a short flowing
  paragraph over a bulleted/numbered list by default: a weaker version of
  this rule risks being ignored, since Qwen3-8B's own bias toward
  structured output for multi-point answers is strong enough that the
  rule needs to be explicit and give a worked example, not just say "be
  concise".
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
  a page refresh mid-answer can't lose it — a StreamingResponse-owned
  generator would otherwise leave a real question with zero assistant
  rows for either attempt if the client disconnects mid-stream. One
  durable, DB-ordered queue (`get_oldest_queued_message`,
  never trust `asyncio` scheduling order for "what's next"); one shared
  "current session" slot + `asyncio.Condition` for live-watching instead
  of a per-subscriber queue registry, since only one generation is ever
  active app-wide. `recover_from_crash()` runs at startup — see
  ARCHITECTURE.md's ["Resilient, queued message
  generation"](ARCHITECTURE.md#resilient-queued-message-generation)
  before touching this file. Document ingestion is deliberately NOT this
  worker's job (see `src/ingestion/worker.py` above) — its own
  `_wait_for_documents_ready` only polls durable state as a correctness
  gate before retrieval, it does no embedding and emits nothing to any
  watcher.
- `src/concurrency.py` — `LockedEmbedder`/`LockedLLMDriver` wrap the real
  embedder/driver, each behind its *own* `asyncio.Lock` (not one shared
  lock — a shared lock would block embedding for the entire length of an
  LLM generation stream), constructed once at startup. Fixes a race
  enabled by `llama_cpp`'s `Llama` having no internal locking: ingestion's
  embedding calls and a query's embedding call could corrupt shared model
  state if they landed concurrently. The separate locks are what make
  Rule #1's "an embedding only waits for a prior embedding, never for an
  LLM" guarantee possible in the first place.
- `src/api/` — FastAPI. `context.py` holds `AppContext` + the
  `get_context` DI function (kept separate from `app.py` to avoid
  circular imports between routes and the app factory).
  `routes_messages.py`'s `post_message` does all its (fast, side-effect-
  light) setup — user message, a `Document` stub per upload via
  `pipeline.create_stub()`, the assistant placeholder — directly in the
  plain handler, before ever constructing a `StreamingResponse`, then
  returns a response that just watches: `_stream_watch` merges
  `_watch_documents` (polls `Document` rows directly, independent of
  either worker) with `context.generation_worker.watch(...)` via
  `merge_async_iterators` (`src/api/sse.py`), so neither stream blocks the
  other. The SSE payload (`document_status`, `message_status`, `token`,
  `citations`, `done`) is unchanged. `GET .../messages/{id}/stream` is the
  reconnect route a refreshed page calls to reattach — it runs the exact
  same `_stream_watch`, since watching is all either route ever does.
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
  proving that block is live. The scripts allowed to touch the network are
  `scripts/download_models.py` and the in-app model downloader
  (`src/download.py`, used by `/api/models/download`); the former is kept
  outside `tests/` so `python scripts/download_models.py` can be run without
  the socket block, while `src/download.py` is exercised only through
  mocked/unit paths.
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
  `tests/test_generation_worker.py`, `tests/test_ingestion_worker.py`, and
  `tests/test_llama_cpp_driver.py` each include at least one test that
  fails if the property being protected is removed (partial-connection-leak
  rollback; citation fabrication; abandoning a watch mid-generation still
  reaching `DONE` with real content; a document finishing ingestion while
  an unrelated conversation is still mid-generation; a `<think>` tag split
  across several streamed chunks) — that's intentional; keep that pattern
  for new safety-critical logic rather than testing only the happy path.
