<p align="center">
  <img src="assets/logo.svg" alt="keepr Logo" width="160">
</p>

<p align="center">
  <strong>A privacy-first, local-first document RAG assistant — your documents, your machine, your answers.</strong>
</p>

<p align="center">
  <a href="README.md">English</a> •
  <a href="docs/README.es.md">Spanish</a> •
  <a href="docs/README.zh.md">简体中文</a> •
  <a href="docs/README.ru.md">Русский</a> •
  <a href="docs/README.fr.md">Français</a>
</p>

<p align="center">
  <a href="https://github.com/pironc/keepr"><img src="https://img.shields.io/github/stars/pironc/keepr?style=social" alt="GitHub stars" data-canonical-src="https://img.shields.io/github/stars/pironc/keepr?style=social" style="max-width: 100%;"></a>
  <a href="https://github.com/pironc/keepr"><img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="License"></a>
  <a href="https://github.com/pironc/keepr"><img src="https://img.shields.io/badge/100%25-Local-FF6600" alt="100% Local"></a>
  <a href="https://github.com/pironc/keepr"><img src="https://img.shields.io/github/issues/pironc/keepr?style=flat-square&color=blue" alt="Issues"></a>
</p>

<p align="center">
  <a href="https://github.com/pironc/keepr/releases/latest/download/keepr-mac-aarch64.dmg"><img src="https://img.shields.io/badge/OSX_ARM-FF6600?logo=apple&logoColor=white" alt="macOS Apple Silicon"></a>
  <a href="https://github.com/pironc/keepr/releases/latest/download/keepr-mac-x86_64.dmg"><img src="https://img.shields.io/badge/OSX_x86-FF6600?logo=apple&logoColor=white" alt="macOS Intel"></a>
  <a href="https://github.com/pironc/keepr/releases/latest/download/keepr-linux-x86_64.AppImage"><img src="https://img.shields.io/badge/Linux-FF6600?logo=linux&logoColor=white" alt="Linux"></a>
  <a href="https://github.com/pironc/keepr/releases/latest/download/keepr-windows-x86_64-setup.exe"><img src="https://img.shields.io/badge/Windows-FF6600?logo=data:image/svg%2Bxml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA4OCA4OCIgZmlsbD0iI2ZmZmZmZiI+CiAgPHBhdGggZD0iTTAgMTIuNDAybDM1LjY4Ny00Ljg2LjAxNiAzNC40MjMtMzUuNjcuMjAzeiIvPgogIDxwYXRoIGQ9Ik0zNS42NyA0NS45MzFsLjAyOCAzNC40NTNMLjAyOCA3NS40OC4wMjYgNDUuN3oiLz4KICA8cGF0aCBkPSJNNDAuMDAyIDcuMzc3TDg3LjMxNCAwdjQxLjUyN2wtNDcuMzE4LjM3NnoiLz4KICA8cGF0aCBkPSJNODcuMzMxIDQ1LjkwNmwtLjAxMSA0MS4zNC00Ny4zMTgtNi42NzgtLjA2Ni0zNC43Mzl6Ii8+Cjwvc3ZnPgo=" alt="Windows"></a>
</p>

# keepr

**keepr** is a retrieval-augmented generation (RAG) chat application that runs entirely on your own machine. Drop in PDFs or text files, ask questions, and get answers grounded in — and only in — what you uploaded, with inline citations back to the exact source passage. No cloud API, no data leaving your device, fully functional with the network switched off once the models are downloaded.

<p align="center">
  <img src="assets/keepr.jpg" alt="keepr — privacy-first document RAG assistant" width="820">
</p>

## Table of Contents

- [Why keepr?](#why-keepr)
- [Features](#features)
- [Getting Started](#getting-started)
  - [Docker](#docker)
  - [Local Development](#local-development)
  - [Environment Variables](#environment-variables)
  - [Running with Real Local Models](#running-with-real-local-models)
  - [Native Desktop App (macOS)](#native-desktop-app-macos)
- [Development](#development)
  - [Project Structure](#project-structure)
  - [Commands](#commands)
  - [Testing](#testing)
  - [Troubleshooting](#troubleshooting)
- [License](#license)

For the technology rationale behind every choice, the full system design, the wire format, the scaling story, and the design principles, see **[ARCHITECTURE.md](ARCHITECTURE.md)**.

## Why keepr?

Most RAG tools either send your documents to a cloud API (privacy risk) or wrap a black-box local server like Ollama (opaque internals). keepr takes the third path: **every layer is hand-rolled, documented, and owned by you** — from the vector index math to the LLM token streaming. The thesis is that a personal-scale RAG system doesn't need distributed infrastructure; it needs clear, correct code you can read and understand end-to-end.

- **Your data never leaves your machine.** No telemetry, no cloud API keys, no network calls during inference.
- **Every technology choice has a documented, specific reason** — not "because it's popular." See [ARCHITECTURE.md](ARCHITECTURE.md) for the full rationale behind each decision.
- **The anti-hallucination mechanism is deterministic** (a float comparison against a similarity threshold), not a prompt asking a 7-8B model to self-police.
- **Designed to scale** from a laptop to a dedicated server without a rewrite — every layer is behind a named interface with exactly one concrete implementation today, ready for a second when needed.

## Features

### Privacy & air-gapped operation
- `LLM_DRIVER=mock` / `EMBEDDER=mock` (the defaults) run the full ingestion → retrieval → citation pipeline with zero model downloads, testable in milliseconds; real inference is `llama-cpp-python` on GGUF models (Metal, CUDA, or CPU).
- The frontend is dependency-free vanilla HTML/CSS/JS — no CDN script, no npm build, no icon font — with fonts and icons vendored locally as `.woff2`/`.svg` files.
- The test suite runs under `pytest-socket` with all real network access blocked, including a positive-control test that fails if the block is ever silently disabled.

### Deterministic anti-hallucination
- Retrieval is scoped to the documents in that conversation, never a global index or the model's pre-training data.
- Below a cosine-similarity threshold, checked per-chunk (not just the best match), keepr refuses *before* the LLM is called (`src/rag/engine.py`) — a `float` comparison, not a judgment handed to the model.
- After generation, every `[chunk_N]` citation is checked by set membership against the chunk IDs actually retrieved that turn, so a fabricated citation is rejected rather than trusted.

### Conversations & documents
- Each conversation is its own retrieval scope — like a Claude Project or a NotebookLM notebook — with sidebar search, pinning, and inline rename/delete via a context menu.
- Documents are deduplicated by SHA-256 content hash — re-attaching the same file is a no-op, not a duplicated set of chunks.

### Durable background workers
- Ingestion (`IngestionWorker`) and generation (`GenerationWorker`) each run on their own DB-driven queue independent of any HTTP connection, so an embedding only ever waits for a prior embedding, never for an unrelated LLM generation. Closing the tab, losing network, or refreshing mid-answer never pauses or loses work — reopening reattaches to it live over SSE.
- On restart, anything left mid-processing by a prior crash is marked errored with its partial content preserved, rather than left spinning forever.
- Every file type goes through the same `Ingestor` protocol — per-file status (`staged → extracting → chunking → embedding → indexed`) driven by real SSE events, not a fake timer — which is what makes a future format (e.g. audio/video, currently a stubbed `AudioVideoIngestor` that raises `UnsupportedSourceError` rather than a crash or silent no-op) addable without touching anything downstream.

### In-app model management
- Settings lists every `.gguf` in `models/`, classified as LLM or embedding by reading its own metadata (a pooling-layer key), never by filename.
- Models download straight from Hugging Face Hub with live progress; switching the active model persists the choice and restarts the app so the new model actually loads — deleting a model file works the same way.

### Pluggable architecture
Every layer sits behind a protocol/ABC — swap implementations without restructuring:

| Layer | Interface | Implementations |
|---|---|---|
| **LLM Driver** | `LLMDriver` | `MockLLMDriver` (deterministic, zero-download), `LlamaCppDriver` (real GGUF inference) |
| **Embedder** | `Embedder` | `MockEmbedder` (hashing-trick bag-of-words), `LlamaCppEmbedder` (nomic-embed-text-v2-moe, multilingual) |
| **Vector Index** | `VectorIndex` | `NumpyFlatIndex` (float32, exact), `QuantizedNumpyFlatIndex` (int8 scalar quant, ~4× less memory) |
| **Ingestor** | `Ingestor` | `PdfIngestor`, `TextIngestor`, `AudioVideoIngestor` (clean stub — raises `UnsupportedSourceError`) |
| **Storage** | `Repository` | SQLite via `aiosqlite` (the only module that speaks SQL — a Postgres swap is a one-file change) |

### Native desktop app
- A [Tauri](https://tauri.app/) v2 wrapper bundles a PyInstaller-compiled backend into native installers — macOS `.app`/`.dmg`, Windows `.exe`/`.msi`, Linux `.AppImage`/`.deb` — via a CI build matrix, no Python install required on the target machine.
- The same codebase runs as a web app (`make run`, hot reload) or a native desktop build (`make tauri-build`).

---

## Getting Started

### Docker

The fastest way to get a running instance — no Python setup needed:

**Prerequisites:** [Docker](https://docs.docker.com/get-docker/) with Compose v2.

```bash
git clone https://github.com/pironc/keepr.git
cd keepr
docker compose up --build
```

Open [http://localhost:8000](http://localhost:8000). Drop a PDF or `.txt` file into the chat and ask a question. The Compose file defaults to `LLM_DRIVER=mock` / `EMBEDDER=mock` — the entire pipeline runs with zero model downloads.

For real local inference, mount your models and switch the drivers:

```bash
# First, download GGUF models to models/ (one-time):
python scripts/download_models.py

# Then override the Compose environment:
LLM_DRIVER=llama_cpp EMBEDDER=llama_cpp docker compose up --build
```

### Local Development

Run natively with hot reload — no Docker needed:

**Prerequisites:** Python 3.12+.

```bash
git clone https://github.com/pironc/keepr.git
cd keepr

make install
source .venv/bin/activate
make run
```

Open [http://localhost:8000](http://localhost:8000). The defaults (`LLM_DRIVER=mock`, `EMBEDDER=mock`) need zero downloads — the whole ingestion → retrieval → citation pipeline is testable immediately.

### Environment Variables

Copy `.env.example` to `.env` to customize. The essentials:

| Variable | Purpose |
|---|---|
| `LLM_DRIVER` | `mock` or `llama_cpp`. Leave unset to auto-detect: `llama_cpp` once a real GGUF is actually present (e.g. downloaded/selected in Settings), `mock` otherwise — zero-download by default on a fresh checkout, no env var needed once a model exists. |
| `LLM_MODEL_PATH` | Path to GGUF file. Only when `LLM_DRIVER=llama_cpp`. Leave unset to use the model picked in the Settings menu. |
| `LLM_CONTEXT_WINDOW` | Context window size (default auto-detected from `MEMORY_TIER`, typically `8192`). |
| `LLM_GPU_LAYERS` | Layers to offload to GPU (`-1` = all, default). |
| `EMBEDDER` | `mock` or `llama_cpp`. Same auto-detect as `LLM_DRIVER`, evaluated independently. |
| `EMBEDDING_MODEL_PATH` | Path to GGUF embedding model. Only when `EMBEDDER=llama_cpp`. Leave unset to use the model picked in the Settings menu. |
| `EMBEDDING_GPU_LAYERS` | Embedding GPU layers (default `0` — embedding runs on CPU to avoid GPU contention with the LLM). |
| `VECTOR_INDEX_BACKEND` | `flat` (float32, exact) or `quantized` (int8 scalar quant, ~4× less memory). |
| `RETRIEVAL_TOP_K` | Number of chunks to retrieve per query (default `5`). |
| `RETRIEVAL_MIN_SIMILARITY` | Cosine similarity threshold below which the engine refuses to answer (default `0.22` — calibrated against nomic-embed-text-v2-moe). |
| `MODELS_DIR` | Directory scanned for `.gguf` model files (default `models`). |
| `DATABASE_PATH` | SQLite database path (default `data/keepr.db`). |
| `INDEX_DIR` | Per-conversation vector index directory (default `data/index`). |
| `UPLOAD_DIR` | Uploaded file storage (default `data/uploads`). |
| `MEMORY_TIER` | Override auto-detected memory budget (`minimal`, `standard`, `large`, `server`). Leave unset for auto-detection. |
| `CHUNK_SIZE` | Characters per chunk (default `800`). |
| `CHUNK_OVERLAP` | Overlap between consecutive chunks (default `150`). |
| `LOG_LEVEL` | Logging level (default `INFO`). |

### Running with Real Local Models

Install the `llama_cpp` driver — a separate, heavier extra (compiles `llama-cpp-python` from source; on Apple Silicon this picks up Metal acceleration automatically, no extra flags needed):

```bash
pip install -e ".[llama]"
```

Then fetch the default Qwen3-8B + nomic-embed-text-v2-moe GGUF pair (~7.5 GB total) either from the app's **Settings** menu, or from the CLI:

```bash
python scripts/download_models.py
```

Then in `.env`:

```env
LLM_DRIVER=llama_cpp
EMBEDDER=llama_cpp
```

Restart `make run`. The first message will be slower (model load into memory); everything after that runs from the resident warm stack.

To use a different model, drop any llama.cpp-compatible GGUF into `models/` and pick it in the **Settings** menu — the choice is saved and applied on the next restart. You can also set `LLM_MODEL_PATH` / `EMBEDDING_MODEL_PATH` directly to override it. (The embedding model is less freely swappable: `RETRIEVAL_MIN_SIMILARITY` is calibrated to nomic-embed-text-v2-moe, so changing it means recalibrating that threshold.)

### Native Desktop App (macOS)

Prebuilt: grab the `.dmg` from the download badges above, or install via [Homebrew](https://brew.sh):

```bash
brew tap pironc/keepr https://github.com/pironc/keepr
brew install --cask keepr
```

The Cask formula ([`Casks/keepr.rb`](Casks/keepr.rb)) always tracks the latest GitHub release — `brew upgrade --cask keepr` picks up new builds the same way.

Build from source:

```bash
# Development mode — opens a native window pointing at http://localhost:8000.
# The Python backend must already be running (`make run` in another terminal).
make tauri-dev

# Production build — compiles the Python backend to a standalone binary,
# bundles it into a .app, and produces a .dmg installer.
make tauri-build
```

The compiled `.app` requires no Python installation on the target machine — the backend is a self-contained PyInstaller binary embedded inside the bundle. keepr isn't code-signed/notarized yet, so macOS will call it "damaged" on first open regardless of install method; the DMG's own background image (and `Casks/keepr.rb`'s caveat) both point at the fix: `xattr -cr /Applications/keepr.app`.

---

## Development

### Project Structure

```
keepr/
├── src/
│   ├── models.py              # Pydantic v2 models — single source of truth
│   ├── config.py              # Hardware detection, memory tiers, Settings
│   ├── concurrency.py         # LockedEmbedder / LockedLLMDriver wrappers
│   ├── logger.py              # Structured logging
│   ├── download.py            # Model-download helpers (Hugging Face Hub, shared with the CLI)
│   ├── gguf_meta.py           # Lightweight GGUF metadata classifier (pooling-type detection)
│   ├── model_unavailable.py   # ModelUnavailableError — missing/broken GGUF file, by role
│   ├── api/
│   │   ├── app.py             # FastAPI app factory + lifespan
│   │   ├── context.py         # AppContext + DI
│   │   ├── routes_conversations.py
│   │   ├── routes_messages.py # Multiplexed SSE endpoint
│   │   ├── routes_models.py   # Model status / select / download / quit endpoints
│   │   └── sse.py             # SSE formatting helpers
│   ├── db/
│   │   ├── pool.py            # SQLiteConnectionPool
│   │   ├── repository.py      # The ONLY module that speaks SQL
│   │   └── schema.py          # CREATE TABLE statements
│   ├── embeddings/
│   │   ├── base.py            # Embedder protocol
│   │   ├── mock_embedder.py   # Deterministic hashing-trick embedder
│   │   ├── llama_cpp_embedder.py  # Real GGUF embedding
│   │   └── factory.py
│   ├── ingestion/
│   │   ├── base.py            # Ingestor protocol
│   │   ├── pipeline.py        # Orchestrates extract→chunk→embed→index
│   │   ├── worker.py          # Background task — durable, connection-independent
│   │   ├── chunker.py         # Text chunking with overlap
│   │   ├── registry.py        # Finds the right ingestor for a file
│   │   ├── pdf_ingestor.py
│   │   ├── text_ingestor.py
│   │   └── audio_video_ingestor.py  # Clean stub — raises UnsupportedSourceError
│   ├── llm/
│   │   ├── base.py            # LLMDriver ABC (async token stream)
│   │   ├── mock_driver.py     # Deterministic mock for tests
│   │   ├── llama_cpp_driver.py    # Real GGUF inference with thinking-strip
│   │   └── factory.py
│   ├── rag/
│   │   ├── engine.py          # Core RAG: retrieval + threshold + generation + citation verify
│   │   ├── prompts.py         # Grounding system prompt
│   │   ├── generation_worker.py   # Background task — durable, connection-independent
│   │   ├── index_manager.py   # Per-conversation vector index cache
│   │   ├── greeting.py        # Fast-path greeting/farewell detection
│   │   └── title.py           # LLM-generated conversation titles
│   ├── vectorstore/
│   │   ├── base.py            # VectorIndex protocol
│   │   ├── flat_index.py      # NumpyFlatIndex (float32, exact)
│   │   ├── quantized_flat_index.py  # int8 scalar quantization
│   │   ├── similarity.py      # L2 normalization
│   │   └── factory.py
│   └── web/                   # Vanilla HTML/CSS/JS — zero external dependencies
│       ├── templates/index.html
│       └── static/
│           ├── app.js
│           ├── app.css
│           └── fonts/         # Vendored .woff2 files (no Google Fonts CDN)
├── src-tauri/                 # Tauri v2 native desktop wrapper (Rust)
│   ├── Cargo.toml
│   ├── tauri.conf.json
│   ├── src/lib.rs
│   └── binaries/              # PyInstaller-compiled backend binary
├── tests/                     # pytest + pytest-asyncio + pytest-socket
├── models/                    # Downloaded .gguf model weights (MODELS_DIR)
├── scripts/
│   └── download_models.py     # One-time GGUF model fetcher (test-exempt network script)
├── assets/                    # Logo, screenshots
│   └── icons/                 # White brand icons for the download buttons
├── ARCHITECTURE.md            # Full technical design & scaling story
├── CLAUDE.md                  # AI assistant / contributor guidance
├── Makefile
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

### Commands

```bash
make install          # pip install -e ".[dev]"
make run              # uvicorn src.api.app:app --reload --port 8000
make test             # pytest
make lint             # ruff check .
make typecheck        # mypy --strict
make ci               # lint + typecheck + test — run before considering anything done
make clean            # wipes DB/index/uploads (app state) — NOT model weights
make clean-models     # wipes models/ too
make wipe             # full reset to a just-cloned state (venv, caches, build
                       # output, src-tauri/target, etc.) — models/ still exempt
```

### Testing

```bash
make ci   # ruff check . && mypy && pytest
```

The test suite runs with network access disabled globally (`pytest-socket`) — any test that opens a real network socket fails the whole suite. Mock drivers and embedders make tests fast and deterministic (no model downloads, no flaky API calls). `asyncio_mode = "auto"` means `async def test_...` just works.

Key test files:
- `tests/test_rag_engine.py` — threshold-based refusal, citation verification (including a driver that deliberately fabricates a citation)
- `tests/test_generation_worker.py` — connection-independent generation, crash recovery, watcher reattachment
- `tests/test_ingestion_worker.py` — connection-independent ingestion, crash recovery, idle-unload timing
- `tests/test_ingestion_pipeline.py` — content-hash dedup, status transitions
- `tests/test_vectorstore.py` — quantization memory ratio & top-1 agreement benchmarks
- `tests/test_airgapped.py` — positive control proving the socket block is live
- `tests/test_llama_cpp_driver.py` — thinking-strip across split tags, no model needed

### Troubleshooting

**Answers seem unrelated to my documents**

The retrieval similarity might be below the threshold — check the server logs for the actual cosine similarity score and compare against `RETRIEVAL_MIN_SIMILARITY`. If you're using a different embedder than nomic-embed-text-v2-moe, recalibrate the threshold (see `ARCHITECTURE.md`).

**Mock driver gives generic responses**

The mock driver parses `[chunk_N]` tags from the system prompt for deterministic responses. If no chunks were retrieved (empty index), it will refuse. Add a document first.

**`make run` fails with an import error**

Run `make install` first — the package needs to be installed in editable mode.

---

## License

keepr is licensed under the [MIT License](LICENSE).

Generated with [Claude Code](https://claude.com/claude-code)
