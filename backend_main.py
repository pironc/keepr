"""Entry point compiled to a standalone binary by PyInstaller.

When running as a frozen bundle (``sys.frozen``), the web directory lives
inside ``sys._MEIPASS``; when running from source the web directory is
resolved relative to this file.

``KEEPR_FROZEN`` (read by ``src/config.py``'s ``_default_driver``) makes the
frozen path always default to the real ``llama_cpp`` driver/embedder, never
mock — an end user with no model downloaded yet must see the app's real
"no model installed" refusal, not a meaningless mock-generated answer. An
explicit ``LLM_DRIVER``/``EMBEDDER`` env var still overrides this.
"""

from __future__ import annotations

import multiprocessing
import os
import sys

_FROZEN = getattr(sys, "frozen", False)

if _FROZEN:
    # PyInstaller bundle — web files live under the extraction root.
    os.environ.setdefault("KEEPR_WEB_DIR", os.path.join(sys._MEIPASS, "src", "web"))  # type: ignore[attr-defined]
    # Safe defaults for a self-contained .app: mock drivers, no dotenv.
    os.environ["KEEPR_FROZEN"] = "1"
else:
    # Running from source — resolve paths relative to this file.
    os.environ.setdefault(
        "KEEPR_WEB_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "web"),
    )

import uvicorn  # noqa: E402

from src.api.app import app  # noqa: E402

if __name__ == "__main__":
    # On a frozen build, multiprocessing's "spawn" start method (macOS and
    # Windows) re-launches this executable to become a child process — e.g.
    # src/download.py's model downloader. freeze_support() lets it recognize
    # that re-launch instead of booting a second full backend on the same
    # port. No-op when running from source.
    multiprocessing.freeze_support()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=8000,
            log_level="warning",
        )
    )
    # Read by request_self_quit() (src/api/routes_models.py) for a graceful
    # shutdown that works the same on every platform.
    app.state.uvicorn_server = server
    server.run()
