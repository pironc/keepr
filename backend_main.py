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
import socket
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

# The loopback address/port the backend serves on — mirrored here so the
# pre-bind self-check below can probe it without importing uvicorn internals.
_HTTP_HOST = "127.0.0.1"
_HTTP_PORT = 8000


def port_in_use(host: str = _HTTP_HOST, port: int = _HTTP_PORT) -> bool:
    """True if ``host:port`` can't be bound because something else holds it.

    A best-effort pre-flight check (with an inherent TOCTOU gap — uvicorn
    still fails fast on its own bind). Its real job is to make the "port busy"
    case fail fast with an unambiguous log line so the Tauri shell can spot the
    early child exit and tell the user immediately, rather than both of them
    waiting out the full health time-out.
    """
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
        finally:
            probe.close()
    except OSError:
        return True
    return False


if __name__ == "__main__":
    # On a frozen build, multiprocessing's "spawn" start method (macOS and
    # Windows) re-launches this executable to become a child process — e.g.
    # src/download.py's model downloader. freeze_support() lets it recognize
    # that re-launch instead of booting a second full backend on the same
    # port. No-op when running from source.
    multiprocessing.freeze_support()

    # Fast pre-bind check: if 127.0.0.1:8000 is already taken (almost always a
    # left-over keepr backend from a previous launch that didn't shut down),
    # exit now with a clear message instead of letting the shell wait through
    # its full health-check time-out. The message is deliberately specific so a
    # user reading backend.log knows what to do.
    if port_in_use():
        print(
            f"keepr: 127.0.0.1:{_HTTP_PORT} is already in use — another keepr instance or "
            "process is bound there. Close it and relaunch.",
            file=sys.stderr,
        )
        sys.exit(1)

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=_HTTP_HOST,
            port=_HTTP_PORT,
            log_level="warning",
        )
    )
    # Read by request_self_quit() (src/api/routes_models.py) for a graceful
    # shutdown that works the same on every platform.
    app.state.uvicorn_server = server
    server.run()
