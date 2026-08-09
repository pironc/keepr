"""Ingestor protocol: the extraction boundary that makes this app growable.

Everything downstream of `extract()` — chunking, embedding, indexing,
citations — is uniform regardless of source type. Adding real audio/video
support later means implementing this one method for a new source, not
touching anything else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from src.models import TextSegment


class UnsupportedSourceError(Exception):
    """A file type is recognized but its extraction isn't implemented yet."""


class Ingestor(Protocol):
    def supports(self, filename: str, mime_type: str) -> bool: ...

    async def extract(self, path: Path) -> list[TextSegment]: ...
