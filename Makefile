.PHONY: install run test lint typecheck ci clean clean-models wipe clean-app clean-webview tauri-dev tauri-build build-backend

VENV := .venv/bin
PYTHON := $(VENV)/python3
PIP := $(VENV)/pip

ARCH := $(shell uname -m)
ifeq ($(ARCH),arm64)
	TARGET_TRIPLE := aarch64-apple-darwin
else
	TARGET_TRIPLE := x86_64-apple-darwin
endif

install:
	@test -d .venv || python3 -m venv .venv
	$(PIP) install -e ".[dev]"

run:
	$(VENV)/uvicorn src.api.app:app --reload --reload-dir src --reload-dir src/web --port 8000

test:
	$(VENV)/pytest

lint:
	$(VENV)/ruff check .

typecheck:
	$(VENV)/mypy

ci: lint typecheck test

clean:
	rm -rf data/keepr.db data/index data/uploads
	find . -type d -name "__pycache__" -not -path "./.git/*" -exec rm -rf {} +

# Separate from `clean`: model weights are a multi-GB one-time download,
# not app state — wiping them on every "start fresh" would force a
# re-download users didn't ask for. Run both together (`make clean
# clean-models`) to also drop the models; `make wipe` below is the full
# from-scratch reset (everything except models/).
clean-models:
	rm -rf models

# Full reset to a "just cloned the repo" state: every build artifact,
# cache, installed dependency, and app-data file this repo can accumulate
# — everything `clean` covers, plus the venv, tool caches, Python/PyInstaller
# build output, and the Rust/Tauri build tree. models/ is the one deliberate
# exception (see clean-models above — a multi-GB re-download is its own
# explicit step, never implied by "clean"). Editor/tool-local dirs
# (.vscode/, .idea/, .claude/) and .env are left alone too: those hold your
# own local config, not repo build state, and a fresh clone wouldn't touch
# them either (well, a `.env` you'd created since — a real fresh clone
# wouldn't have one at all — is exactly the kind of thing worth a second
# thought before an automated target deletes it, so this one just doesn't).
wipe: clean
	rm -rf data
	rm -rf .venv venv
	rm -rf .mypy_cache .ruff_cache .pytest_cache
	find . -iname "*.egg-info" -not -path "./.git/*" -exec rm -rf {} +
	rm -rf build dist outputs test-results
	find . -name "*.spec" -not -path "./.git/*" -delete
	rm -rf src-tauri/target src-tauri/gen src-tauri/binaries
	@echo "✓ Wiped to a fresh-clone state (models/ untouched — see clean-models). Run 'make install' to start over."

# Wipes the macOS app data directory — everything the native Tauri
# wrapper persisted (database, index, uploads).  Safe to run while
# the app is closed; won't touch model weights or browser-mode data.
APP_DATA := $(HOME)/Library/Application Support/com.keepr.keepr
clean-app:
	rm -rf "$(APP_DATA)"

# Wipes the desktop (Tauri/WKWebView) *asset* cache only — the NetworkCache
# where a stale copy of app.js/app.css can live. Distinct from clean-app: this
# does NOT touch the database/index/uploads. Needs no rebuild; run it, then
# relaunch `make tauri-dev` to force a cold fetch of the web assets. Both the
# dev `keepr` identifier and the bundled `com.keepr.keepr` are covered.
WEBVIEW_CACHE_DIRS := $(HOME)/Library/Caches/keepr $(HOME)/Library/Caches/com.keepr.keepr
clean-webview:
	rm -rf $(WEBVIEW_CACHE_DIRS)

# Tauri dev — opens a native window pointing at http://localhost:8000.  The
# beforeDevCommand starts its OWN backend on port 8000 (uvicorn --reload), so
# run it alone: do NOT `make run` in another terminal first, or the two will
# fight over port 8000 ("[Errno 48] Address already in use").
# tauri.conf.json declares `binaries/keepr-backend-*` as a bundle resource, and
# Tauri's build script hard-fails if that glob matches nothing — even in dev,
# which never actually runs this file (it spawns its own backend above via
# beforeDevCommand). `make wipe` deletes src-tauri/binaries/ entirely, so
# without this guard every wipe -> tauri-dev cycle hits that build error.
# A real backend (from `make build-backend`) already matches the glob, so this
# never overwrites one — it only fills the gap when nothing does.
tauri-dev:
	@mkdir -p src-tauri/binaries
	@ls src-tauri/binaries/keepr-backend-* >/dev/null 2>&1 || touch src-tauri/binaries/keepr-backend-$(TARGET_TRIPLE)
	cd src-tauri && PATH="$(CURDIR)/$(VENV):$$PATH" cargo tauri dev

# Tauri production build — PyInstaller backend + clean + produce a .app bundle and .dmg.
# The Python backend is compiled to a standalone binary and embedded inside the .app —
# no Python installation required on the target machine.
tauri-build: build-backend
	cd src-tauri && cargo clean
	cd src-tauri && cargo tauri build

# Build the standalone backend binary with PyInstaller.
# Places it at src-tauri/binaries/keepr-backend-{target-triple} so Tauri
# bundles it as an external binary inside the .app.
build-backend:
	$(PIP) install pyinstaller
	$(VENV)/pyinstaller --onefile --name keepr-backend \
		--add-data "src/web:src/web" \
		--hidden-import pypdf \
		--hidden-import aiosqlite \
		backend_main.py
	mkdir -p src-tauri/binaries
	cp dist/keepr-backend src-tauri/binaries/keepr-backend-$(TARGET_TRIPLE)
	@echo "✓ backend binary → src-tauri/binaries/keepr-backend-$(TARGET_TRIPLE)"
