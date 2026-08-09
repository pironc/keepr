"""PDF text extraction, one TextSegment per page."""

from __future__ import annotations

import asyncio
from pathlib import Path

from pypdf import PdfReader

from src.models import PageRef, TextSegment


class PdfIngestor:
    def supports(self, filename: str, mime_type: str) -> bool:
        return filename.lower().endswith(".pdf") or mime_type == "application/pdf"

    async def extract(self, path: Path) -> list[TextSegment]:
        return await asyncio.to_thread(_extract_sync, path)


def _extract_sync(path: Path) -> list[TextSegment]:
    reader = PdfReader(path)
    segments: list[TextSegment] = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            segments.append(TextSegment(text=text, source_ref=PageRef(page=page_number)))
    return segments
