"""Tests for the Ingestor implementations and the registry that picks them."""

from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfWriter

from src.ingestion.audio_video_ingestor import AudioVideoIngestor
from src.ingestion.base import UnsupportedSourceError
from src.ingestion.pdf_ingestor import PdfIngestor
from src.ingestion.registry import find_ingestor
from src.ingestion.text_ingestor import TextIngestor
from src.models import PageRef


async def test_text_ingestor_extracts_single_page(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("hello world", encoding="utf-8")

    segments = await TextIngestor().extract(path)

    assert len(segments) == 1
    assert segments[0].text == "hello world"
    assert segments[0].source_ref == PageRef(page=1)


async def test_text_ingestor_skips_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.txt"
    path.write_text("   \n  ", encoding="utf-8")

    assert await TextIngestor().extract(path) == []


def test_pdf_ingestor_supports_by_extension() -> None:
    ingestor = PdfIngestor()
    assert ingestor.supports("report.pdf", "application/octet-stream")
    assert not ingestor.supports("notes.txt", "text/plain")


async def test_pdf_ingestor_skips_pages_with_no_extractable_text(tmp_path: Path) -> None:
    path = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with path.open("wb") as handle:
        writer.write(handle)

    assert await PdfIngestor().extract(path) == []


def test_audio_video_ingestor_supports_by_extension_and_mime() -> None:
    ingestor = AudioVideoIngestor()
    assert ingestor.supports("meeting.mp4", "video/mp4")
    assert ingestor.supports("call.mp3", "audio/mpeg")
    assert not ingestor.supports("notes.txt", "text/plain")


async def test_audio_video_ingestor_raises_unsupported_not_crashes(tmp_path: Path) -> None:
    path = tmp_path / "call.mp3"
    path.write_bytes(b"not a real audio file")

    with pytest.raises(UnsupportedSourceError):
        await AudioVideoIngestor().extract(path)


def test_registry_picks_text_ingestor_for_txt() -> None:
    assert isinstance(find_ingestor("notes.txt", "text/plain"), TextIngestor)


def test_registry_picks_pdf_ingestor_for_pdf() -> None:
    assert isinstance(find_ingestor("report.pdf", "application/pdf"), PdfIngestor)


def test_registry_picks_audio_video_ingestor_for_mp4() -> None:
    assert isinstance(find_ingestor("meeting.mp4", "video/mp4"), AudioVideoIngestor)


def test_registry_returns_none_for_unknown_type() -> None:
    assert find_ingestor("archive.zip", "application/zip") is None
