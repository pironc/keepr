"""Picks the right Ingestor for a given file, by extension or MIME type."""

from __future__ import annotations

from src.ingestion.audio_video_ingestor import AudioVideoIngestor
from src.ingestion.base import Ingestor
from src.ingestion.pdf_ingestor import PdfIngestor
from src.ingestion.text_ingestor import TextIngestor

_INGESTORS: tuple[Ingestor, ...] = (PdfIngestor(), TextIngestor(), AudioVideoIngestor())


def find_ingestor(filename: str, mime_type: str) -> Ingestor | None:
    for ingestor in _INGESTORS:
        if ingestor.supports(filename, mime_type):
            return ingestor
    return None
