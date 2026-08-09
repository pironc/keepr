"""Audio/video ingestion — deliberately not implemented yet.

This class exists so the file-type routing, UI messaging, and citation
schema (`TimeRef` already exists in `src.models`) are all exercised
end-to-end today, with a clean, expected failure rather than a silent
no-op or a crash. Implementing real transcription later (see
ARCHITECTURE.md's v2 roadmap — faster-whisper, lazy-loaded, purged after
use) means filling in `extract()` here; nothing upstream or downstream
changes.
"""

from __future__ import annotations

from pathlib import Path

from src.ingestion.base import UnsupportedSourceError
from src.models import TextSegment

_AUDIO_VIDEO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".mp4", ".mov", ".webm")


class AudioVideoIngestor:
    def supports(self, filename: str, mime_type: str) -> bool:
        return filename.lower().endswith(_AUDIO_VIDEO_EXTENSIONS) or mime_type.startswith(
            ("audio/", "video/")
        )

    async def extract(self, path: Path) -> list[TextSegment]:
        raise UnsupportedSourceError(
            "Audio/video transcription isn't implemented yet (coming in v2 — see "
            "ARCHITECTURE.md). This file type is recognized but not yet supported."
        )
