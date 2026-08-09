"""Tests for text chunking with overlap."""

from __future__ import annotations

import pytest

from src.ingestion.chunker import chunk_segments
from src.models import PageRef, TextSegment


def test_short_segment_is_returned_unchanged() -> None:
    segment = TextSegment(text="short text", source_ref=PageRef(page=1))

    assert chunk_segments([segment], chunk_size=100, chunk_overlap=10) == [segment]


def test_long_segment_is_split_with_overlap() -> None:
    text = "abcdefghij" * 5  # 50 chars
    segment = TextSegment(text=text, source_ref=PageRef(page=3))

    chunks = chunk_segments([segment], chunk_size=20, chunk_overlap=5)

    assert len(chunks) > 1
    assert all(chunk.source_ref == PageRef(page=3) for chunk in chunks)
    # stride is chunk_size - chunk_overlap = 15, so consecutive chunks
    # should share exactly `chunk_overlap` characters at the seam
    assert chunks[0].text[-5:] == chunks[1].text[:5]


def test_chunk_overlap_must_be_smaller_than_chunk_size() -> None:
    segment = TextSegment(text="abc", source_ref=PageRef(page=1))
    with pytest.raises(ValueError):
        chunk_segments([segment], chunk_size=10, chunk_overlap=10)


def test_chunking_never_merges_across_segments() -> None:
    segments = [
        TextSegment(text="page one", source_ref=PageRef(page=1)),
        TextSegment(text="page two", source_ref=PageRef(page=2)),
    ]

    chunks = chunk_segments(segments, chunk_size=100, chunk_overlap=10)

    assert [chunk.source_ref for chunk in chunks] == [PageRef(page=1), PageRef(page=2)]
