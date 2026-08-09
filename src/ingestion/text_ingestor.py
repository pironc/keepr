"""Plain text / Markdown file ingestion, treated as a single page."""

from __future__ import annotations

import asyncio
from pathlib import Path

from src.models import PageRef, TextSegment


class TextIngestor:
    def supports(self, filename: str, mime_type: str) -> bool:
        return filename.lower().endswith((".txt", ".md")) or mime_type.startswith("text/")

    async def extract(self, path: Path) -> list[TextSegment]:
        text = (await asyncio.to_thread(path.read_text, encoding="utf-8")).strip()
        if not text:
            return []
        return [TextSegment(text=text, source_ref=PageRef(page=1))]
