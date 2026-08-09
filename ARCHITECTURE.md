# Architecture

This document explains every non-obvious technology choice in keepr, why
it was made instead of an obvious alternative, and — since that was an
explicit goal — how the design scales from a single laptop to a dedicated
machine without a rewrite. If you only read one section, read
["Scaling past a laptop"](#scaling-past-a-laptop).

## What this actually does, end to end

1. You drop a file into a conversation and hit send. The file is **staged
   client-side only** — nothing is uploaded until you submit.
2. On submit, the file streams to the backend and goes through one
   pipeline regardless of type: **extract → chunk → embed → index**,
   emitting a status event at each stage over the same connection used
   for the answer (`document_status` events drive the UI's per-file
   spinner-to-checkmark animation in real time, not a fake timer).
3. Your question is embedded and compared against every chunk indexed
   *in that conversation* (not globally — see
   [Per-conversation retrieval scope](#per-conversation-retrieval-scope)).
4. **Before any LLM is called**, the best match's similarity score is
   checked against a threshold. Below it, you get a refusal — this is a
   deterministic `float` comparison, not a judgment call handed to a
   7-8B model.
5. Above the threshold, the retrieved chunks are tagged (`[chunk_1]`,
   `[chunk_2]`, ...) and inserted into a grounding system prompt. Tags are
   per-*chunk*, not per-document — if one file dominates the top-k
   results, several tags will legitimately point at different passages of
   that same file, not several different files. The model streams its
   answer token-by-token over SSE.
6. After the stream completes, every citation the model wrote is checked
   against the set of chunk IDs that were *actually retrieved this turn*.
   Anything else is discarded — the model cannot fabricate a citation to
   a document that was never in front of it, because citation validity is
   a set-membership check, not something the model self-reports.
7. None of step 3–6 is actually tied to your browser tab. The moment you
   hit send, `GenerationWorker` (see
   [Resilient, queued message generation](#resilient-queued-message-generation))
   takes over as a background task independent of your connection — if you
   refresh, only your *view* of it disconnects; the answer keeps generating
   and lands in the database regardless, and reloading the page reattaches
   to it, live, wherever it's gotten to.

## Technology choices, and why

| Layer | Choice | Why this, not the obvious alternative |
|---|---|---|
| **LLM serving** | `llama-cpp-python` (GGUF) | Ollama is faster to a demo, but it's a black box for exactly the internals worth understanding (quant format, context window, KV-cache sizing) — and as of its MLX backend switch on Apple Silicon, "how Ollama works" is now platform-dependent. `llama-cpp-python` gives real GGUF/K-quant/mmap internals from one codebase across Metal, CUDA, and CPU. |
| **Model** | `Qwen3-8B`, Q6_K | Swapped from Llama 3.1 8B for the same reason as the embedder above: multilingual support became an explicit requirement. This replaces an earlier version of this row that claimed in-repo "controlled testing" found Llama more reliable than Qwen — that was never true; no eval script, results file, or commit history for such a test exists anywhere in this repo, and the sentence should never have stated it as fact. The actual, sourced (not self-measured) reasoning: Qwen models consistently lead Chinese-language benchmarks by a wide margin (~69 vs. ~44–50 on aggregate Chinese tasks across Gemma/Llama, third-party-reported) and multilingual MMLU (~80 vs. ~72) — a training-data-composition effect, not a marketing one. Q6_K rather than a leaner quant for the same reason as before: memory budget has room to spare. Newer Qwen3.5/3.6 releases moved to bigger-dense or MoE variants, deliberately not chased here — this machine's *unified* memory means an MoE model's inactive experts still occupy real RAM (no separate VRAM pool to offload them to), so the 8B dense model is the better fit for *this* hardware, not just the cautious pick. This choice still isn't backed by an in-repo eval — see the "labeled grounding eval set" gap below, which would close exactly that hole. |
| **Embeddings** | `nomic-embed-text-v2-moe` (GGUF, via the same `llama-cpp-python`) | Multilingual (~100 languages, 8-expert MoE) was the explicit driver — the prior `v1.5` is English-only. Same GGUF path, same 768-dim default, same `search_document:`/`search_query:` prefix convention as v1.5, so this was a same-size (~512MB Q8_0) drop-in swap: no new dependency, no retrieval code path changed. Live-verified: same-language queries score ~0.40–0.54 cosine similarity against genuinely relevant chunks, cross-lingual (English query, French document) still scores ~0.28–0.54 — both comfortably above the recalibrated threshold (see `RETRIEVAL_MIN_SIMILARITY` in `.env.example`), while off-topic queries top out around ~0.20. |
| **Vector store** | Hand-rolled `NumpyFlatIndex` (float32, exact) | At a personal-scale corpus (thousands of chunks), brute-force cosine similarity is *not* a shortcut — it's the technically correct choice: exact (no ANN recall loss), and every line of the math (normalize once, one dot product, `argpartition` top-k) is something explainable end to end, instead of a Chroma/FAISS/Qdrant call that hides it. |
| **Vector quantization** | Hand-rolled `QuantizedNumpyFlatIndex` (int8 scalar) | Rather than reaching for Qdrant's built-in quantization, this is implemented from scratch — per-vector min/max scaling to int8 — because the whole point is owning the technique, not configuring someone else's flag. Measured on this repo's own tests: ~4x memory reduction, 100% top-1 agreement with the float32 index on held-out queries. See `tests/test_vectorstore.py`. |
| **Ingestion** | `Ingestor` protocol, one implementation per source type | The single design decision that makes audio/video "growable, not deferred-forever": every ingestor reduces a file to `TextSegment`s (text + a `PageRef`/`TimeRef` tag); chunking, embedding, indexing, and citation are 100% uniform downstream of that, regardless of source. |
| **PDF parsing** | `pypdf` | Pure Python, no heavy native deps, permissive license (avoids PyMuPDF's AGPL terms). |
| **Storage** | SQLite via `aiosqlite` | Correct for a single local user. See scaling section for the documented Postgres swap. |
| **API / streaming** | FastAPI + one multiplexed SSE stream per exchange | A single transport carries both `document_status` events (driving the ingestion animation) and `token`/`citations`/`done` events (the answer) — one connection, one mental model, instead of a separate polling endpoint for upload progress. |
| **Frontend** | Hand-written vanilla JS/CSS, zero external JS dependency | The obvious choice here (htmx or Alpine.js) was rejected for a concrete reason, not stylistic preference: **loading either from a CDN would silently break the air-gapped claim** on first page load — this app is supposed to still work with the network cut. Vendoring the files avoids the CDN problem but adds a third-party dependency to track for a project whose entire thesis is "own your stack." ~250 lines of plain JS was the more honest trade. |

## Anti-hallucination is deterministic, not prompted

The system prompt (`src/rag/prompts.py`) does ask the model to refuse and
cite sources — but that's the *second* line of defense, not the first.
The first is `src/rag/engine.py`: if the best retrieved chunk's cosine
similarity is below `RETRIEVAL_MIN_SIMILARITY`, the engine returns the
refusal text **without ever constructing a prompt or calling the LLM**.
This means the headline "doesn't hallucinate" claim doesn't rest on
trusting a 7-8B model's judgment — it rests on a `float` comparison you
can read in five lines of code and a test that asserts it
(`tests/test_rag_engine.py`).

Citation verification is the same philosophy applied downstream: after
generation, every `[chunk_N]` tag in the output is checked against the set
of chunk IDs retrieved *for that specific turn*. There's a test
(`test_citation_verification_drops_ids_that_were_never_retrieved`) using a
driver that deliberately fabricates a citation to prove the filter
actually catches it, not just that it exists in the code.

## Per-conversation retrieval scope

Documents are scoped to the conversation they were dropped into — like a
Claude Project or a NotebookLM notebook — not pooled into one global
index. Concretely, each conversation gets its own `VectorIndex`, persisted
to `data/index/{conversation_id}.npz` and cached in memory
(`src/rag/index_manager.py`) after first use. This avoids the "why did it
cite something from an unrelated chat" confusion a single global index
would produce, and keeps the swap-out story (below) contained to one file
per conversation rather than one giant shared structure.

Each `Document` also stores a SHA-256 hash of its raw file content. If the
same file is dropped into a conversation again (e.g. re-attached on a
later message), `IngestionPipeline` recognizes it by content hash and
skips re-ingestion entirely, rather than silently duplicating every one
of its chunks in the index — that duplication was a real bug caught by
comparing a live run against expected output, not something guarded
against from day one.

## Resilient, queued message generation

Originally, the SSE stream driving an answer *was* the thing generating
it: `RagEngine.answer()` ran directly inside the `StreamingResponse`'s
body iterator, and the final `Message` was only written to the database
once, at the very end. Reading Starlette's actual
`StreamingResponse.stream_response`/`__call__`
(`starlette/responses.py`) confirmed that a client disconnect tears that
generator down on its next failed `send()` — so refreshing the page
mid-answer didn't just hide the response, it prevented it from ever being
saved. This was confirmed empirically, not assumed: a real question sent
twice against the production database left **zero** assistant rows for
either attempt.

The fix, in `src/rag/generation_worker.py`: `GenerationWorker` runs one
long-lived background task for the life of the process, processing
messages one at a time from a durable queue. "Durable" is the load-bearing
word — the worker never trusts `asyncio` scheduling or lock-acquisition
order to mean "this is next"; every time it looks for work it queries
`get_oldest_queued_message()` (ordered by `created_at, rowid`). Two
concurrent requests racing to start don't guarantee whichever wins the
race is the one that was actually asked first — deriving order fresh from
durable state every time removes that race entirely. `Message` gained a
`status` column (`QUEUED → RETRIEVING → GENERATING → DONE`/`ERROR`,
mirroring `DocumentStatus`'s existing pattern) so a page load can show
*and reattach to* an in-progress generation instead of showing nothing.

Live-watching reuses a single nullable "current session" slot plus an
`asyncio.Condition`, not a per-subscriber queue registry — deliberately.
Because only one generation is ever active app-wide (there's one shared
LLM instance), any number of watchers (the original sender, a refreshed
tab, a second tab) just track their own read-offset over the same shared,
growing event log. A slow or dead watcher can never back-pressure the
worker: it only ever mutates shared state and calls `notify_all()`, it
never pushes to a subscriber directly. Confirmed live against the real
model: abandoning a connection mid-generation, then reattaching via
`GET .../messages/{id}/stream`, replays everything generated while
disconnected and then continues seamlessly with live, real-time-paced
tokens — the worker never stopped.

Reading the installed `llama_cpp` source directly (not assumed) surfaced
a second, independent bug while building this: the `Llama` class has no
internal locking, and `IngestionPipeline`'s embedding calls already raced
against `RagEngine`'s query-embedding call on the same singleton
`LlamaCppEmbedder` with zero synchronization. `src/concurrency.py` fixes
this structurally — `LockedEmbedder`/`LockedLLMDriver` wrap the real
instances behind one shared `asyncio.Lock` at construction time, so every
call site is forced through it automatically rather than relying on each
new call site to remember to.

A message stuck at `RETRIEVING`/`GENERATING` when the *process itself*
dies (not just a browser refresh) is handled separately:
`GenerationWorker.recover_from_crash()` runs at startup, before the app
serves a single request. `QUEUED` rows are left alone — no side effects
happened yet, and the worker re-derives its queue from the database
regardless. Rows genuinely mid-flight are marked `ERROR` with whatever
partial content had streamed so far preserved, rather than guessed at
being resumed or left spinning forever. Note `make run` uses `--reload`,
so this path runs on every file save during development, not just a rare
production crash.

## Proving "air-gapped," not just claiming it

Every test in the suite runs under `pytest-socket`
(`--disable-socket --allow-unix-socket`, configured in `pyproject.toml`)
— any code path that opened a real network socket would fail the entire
suite, not just a dedicated test. `tests/test_airgapped.py` is the
explicit positive control: it directly attempts a real connection and
asserts it's blocked, so the guard itself is proven live, not just
assumed to be configured correctly. The one deliberate exception is
`scripts/download_models.py`, kept outside `tests/` specifically so it's
never subject to the block — it's the one script allowed to touch the
network, and it's not run as part of `make ci`.

## Scaling past a laptop

Every choice above was picked for a single local user on one machine —
but the point of naming the abstraction boundaries explicitly is that
none of them are *dead ends*. Here's what actually changes if this moved
to a dedicated machine, and what stays exactly the same:

| Concern | On a laptop (today) | On a dedicated machine | What has to change |
|---|---|---|---|
| **Model size / quality** | Qwen3-8B, Q6_K, 8k–16k context (`config.py`'s `standard`/`large` tiers) | A 32B+/70B-class model, Q6_K/Q8_0, 32k+ context | Nothing but the `MEMORY_TIER`/model-path env vars — `config.py`'s tier ladder already has a `server` tier documenting exactly this, and `LLMDriver` never changes. |
| **Vector search** | Hand-rolled flat NumPy index, exact, sub-millisecond at thousands of chunks | An ANN index (Qdrant, LanceDB) once a corpus genuinely exceeds ~500K–1M vectors on one machine, or once concurrent writers/rich metadata filtering are needed | Implement one more `VectorIndex` (same `add`/`search`/`save` interface, `src/vectorstore/base.py`) — `IndexManager` and everything above it is unaffected. |
| **Storage** | SQLite, one file, one local user | Postgres, concurrent multi-user access | `src/db/repository.py` is the *only* module that speaks SQL — swapping the connection layer underneath it (and the schema's SQL dialect) doesn't touch `rag/engine.py`, `api/routes_*.py`, or anything else. |
| **Ingestion concurrency** | One file processed inline per request, in-process | A background job queue (Celery/arq) so uploads don't block a request thread under real concurrent load | `IngestionPipeline.ingest()` already yields discrete status events — wiring those into a job queue instead of a direct async generator is a dispatch change, not a rewrite of the extract→chunk→embed→index logic itself. |
| **Concurrency control** | A single `asyncio.Lock` in `IndexManager`, plus one shared `asyncio.Lock` serializing all embedder/LLM calls (`src/concurrency.py`) — both fine for one user, one process | Per-conversation locks; a real message broker (Redis pub/sub, or similar) once generation needs to run across multiple server processes, not just one | Same interfaces, finer-grained locking. `GenerationWorker`'s in-memory "current session" state is an explicit single-process constraint (documented in its own module) — a multi-process version needs the queue-claim step (`get_oldest_queued_message` → mark `RETRIEVING`) to become a single atomic conditional `UPDATE`, guarding against two workers claiming the same row. |
| **Audio/video** | `AudioVideoIngestor` is a clean, documented stub | Real transcription (`faster-whisper`, lazily loaded, memory-purged after use — see below) | Implement `extract()` on `AudioVideoIngestor`. Chunking, embedding, indexing, and citations (`TimeRef` already exists in `src/models.py`) require zero changes — this is the whole reason the `Ingestor` protocol exists. |

The throughline: every one of these is a **named interface with exactly
one concrete implementation today** (`LLMDriver`, `Embedder`,
`VectorIndex`, `Ingestor`, `Repository`). Scaling up means adding a second
implementation behind an interface that already exists, not restructuring
the system around a new requirement discovered too late.

## Native desktop app (Tauri v2)

The same codebase runs as a web app (browser at `localhost:8000`) or as a
native macOS desktop application — two delivery forms, zero code forks.

The desktop path works like this:

1. **Development:** `make tauri-dev` opens a native Tauri window that points
   at `http://localhost:8000` — the Python backend runs separately (`make
   run`) with hot reload. This is the inner dev loop: edit Python, save,
   the backend restarts; the Tauri window is just a thin shell.

2. **Production:** `make tauri-build` compiles the Python backend to a
   standalone binary via PyInstaller (`backend_main.py` → `keepr-backend`),
   then Tauri bundles that binary as an external resource inside a signed
   `.app` bundle and produces a `.dmg` installer. The target machine needs
   no Python installation — the backend is a self-contained executable
   embedded inside the app.

The Tauri layer (`src-tauri/src/lib.rs`) is deliberately thin: it spawns
the backend binary as a child process on app launch, waits for the health
check endpoint to respond, then points the webview at `http://127.0.0.1:8000`.
On app close, it sends SIGTERM to the backend process. No application logic
lives in Rust — the boundary is purely process management.

**Why Tauri and not Electron:**
- Electron bundles an entire Chromium browser (~100+ MB). Tauri uses the
  platform's native webview (WebKit on macOS, WebView2 on Windows) — binary
  overhead is ~5 MB.
- The Python backend is already self-contained; Tauri's role is strictly a
  window frame + process lifecycle, nothing more. Electron's Node.js runtime
  would be dead weight here — there's no npm build step, no JS bundler, no
  Node backend to bridge to.
- Tauri's Rust core compiles to a native binary with no garbage-collected
  runtime — startup is near-instant, memory footprint is minimal.

**Why PyInstaller and not embedding CPython:**
- PyInstaller (`--onefile`) produces a single self-extracting executable
  that bundles the Python interpreter, all `.py` files, and all native
  dependencies (numpy, llama-cpp-python) into one binary. The Tauri bundle
  just ships that binary as a resource — no `pip install`, no `python3`
  requirement, no virtual environment on the target machine.
- The tradeoff: the compiled binary is ~80-120 MB (mostly numpy + llama-cpp
  shared libraries). This is acceptable for a desktop app distributed as a
  `.dmg`; the alternative (requiring users to install Python + dependencies)
  would be a much higher barrier to entry for non-developers.
- `backend_main.py` is the PyInstaller entry point — it resolves the web
  directory path differently depending on whether it's running frozen
  (`sys._MEIPASS`) or from source, and defaults to mock drivers in frozen
  mode so the app works out of the box with nothing extra installed.

## What's deliberately not built yet

Being explicit about scope, not just implicit through omission:

- **Real audio/video transcription.** `AudioVideoIngestor.extract()`
  raises a clear `UnsupportedSourceError` today. The plan (informed by
  research before building): `faster-whisper`, lazy-loaded only when a
  file needs it (the "cold stack"), explicit `gc.collect()` + backend
  cache purge after use, kept at Q5_K/Q8_0-equivalent precision (not Q4 —
  transcription errors compound into every downstream embedding and
  citation, so this is the one place *not* to over-quantize).
- **Conversation history truncation/summarization.** Long conversations
  currently send full history to the model every turn; this will hit the
  context window eventually and isn't handled yet.
- **BM25/keyword search fused with vector search** (Reciprocal Rank
  Fusion) — cheap to add, catches exact-term/acronym queries embeddings
  miss, deferred to keep v1's surface area focused.
- **File ingestion doesn't have the same disconnect-resilience as message
  generation.** `IngestionPipeline` still runs inline in the request that
  uploaded the file — a refresh mid-upload doesn't lose already-persisted
  chunks (each phase writes to the DB as it completes, same as before),
  but it does leave a document stuck mid-status with nothing left to
  finish it, the same class of gap `GenerationWorker` was built to close
  for messages. The identical pattern (durable queue, background worker,
  reattachable watch) would generalize directly; not done here since it
  wasn't the reported problem.
- **A second, separate pre-existing race, found but not fixed**:
  `IngestionPipeline.ingest()`'s `index.add(...)` mutates the shared
  `NumpyFlatIndex` entirely outside `IndexManager`'s lock (only `.get()`/
  `.save()` take it) — two concurrent uploads to the same conversation
  could still race on that mutation today.
- **A labeled grounding eval set** (30-50 questions across
  answerable/unanswerable/adversarial-injection categories, asserting
  refusal-rate and citation-precision as a CI-enforced regression test) —
  the mechanism (`RagEngine`, citation verification) is tested; the
  broader statistical eval harness is the natural next addition. This is
  also the gap that let an unverified model-choice claim sit in this file
  uncorrected for a while (see the **Model** row above) — a real eval set,
  run across candidate models, would make that kind of claim measured
  instead of asserted.
