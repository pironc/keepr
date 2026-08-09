"""Server-Sent Events formatting."""

from __future__ import annotations

import json
from typing import Any


def format_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
