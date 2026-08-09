.PHONY: install run test lint typecheck ci clean clean-models clean-app download-models tauri-dev tauri-build build-backend

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
	$(PIP) install -e ".[dev]"

download-models:
	$(PIP) install -e ".[models]"
	$(PYTHON) scripts/download_models.py

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
# clean-models`) for a true wipe-everything reset.
clean-models:
	rm -rf data/models

# Wipes the macOS app data directory — everything the native Tauri
# wrapper persisted (database, index, uploads).  Safe to run while
# the app is closed; won't touch model weights or browser-mode data.
APP_DATA := $(HOME)/Library/Application Support/com.keepr.keepr
clean-app:
	rm -rf "$(APP_DATA)"

# Tauri dev — opens a native window pointing at http://localhost:8000.
# The Python backend must already be running (make run in another terminal).
tauri-dev:
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
