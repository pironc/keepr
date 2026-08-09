"""Entry point compiled to a standalone binary by PyInstaller.

When running as a frozen bundle (``sys.frozen``), the web directory lives
inside ``sys._MEIPASS``; when running from source the web directory is
resolved relative to this file.

In the frozen (production) path we default to mock drivers so the app
works out of the box with nothing extra installed.  Users who want real
local models set the usual env vars (``LLM_DRIVER``, ``EMBEDDER``, etc.)
before launching.
"""

from __future__ import annotations

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
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        log_level="warning",
    )
