"""Text chunking with overlap.

Deliberately simple: each output chunk is a sub-slice of exactly one
extracted `TextSegment` (a PDF page, a whole text file), never a merge
across pages. That keeps every chunk's citation honest — "page 4" always
means the text genuinely came from page 4 — at the cost of not packing
short pages together as tightly as a cross-page chunker would.
"""

from __future__ import annotations

from src.models import TextSegment


def chunk_segments(
    segments: list[TextSegment], chunk_size: int, chunk_overlap: int
) -> list[TextSegment]:
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be smaller than chunk_size")

    chunks: list[TextSegment] = []
    for segment in segments:
        chunks.extend(_chunk_one(segment, chunk_size, chunk_overlap))
    return chunks


def _chunk_one(segment: TextSegment, chunk_size: int, chunk_overlap: int) -> list[TextSegment]:
    text = segment.text
    if len(text) <= chunk_size:
        return [segment]

    stride = chunk_size - chunk_overlap
    pieces: list[TextSegment] = []
    start = 0
    while start < len(text):
        piece = text[start : start + chunk_size].strip()
        if piece:
            pieces.append(TextSegment(text=piece, source_ref=segment.source_ref))
        if start + chunk_size >= len(text):
            break
        start += stride
    return pieces
