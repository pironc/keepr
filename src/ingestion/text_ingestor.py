"""Plain text file ingestion, treated as a single page.

`extract()` reads every supported extension the exact same way — raw UTF-8
text, no format-specific parsing (no JSON structure, no Markdown rendering,
no CSV table awareness) — so growing `_TEXT_EXTENSIONS` costs nothing here;
it's purely a `supports()` matter. `process_existing()` (src/ingestion/
pipeline.py) always calls `find_ingestor` with a fixed placeholder mime type,
never the browser's real one, so the extension list below is what actually
decides text-file coverage in practice — the `mime_type` fallback exists for
completeness/future callers, not because it's reachable in the current
pipeline.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from src.models import PageRef, TextSegment

_TEXT_EXTENSIONS = (
    # Plain text / docs
    ".txt", ".md", ".rst", ".log",
    # Structured data
    ".json", ".csv", ".tsv", ".yaml", ".yml", ".xml",
    # Config
    ".ini", ".cfg", ".conf", ".toml",
    # Common source code
    ".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".htm", ".css", ".scss",
    ".sh", ".bash", ".zsh", ".rb", ".go", ".rs", ".java", ".c", ".cpp", ".h",
    ".hpp", ".cs", ".php", ".sql", ".kt", ".swift", ".pl", ".lua",
)


class TextIngestor:
    def supports(self, filename: str, mime_type: str) -> bool:
        return filename.lower().endswith(_TEXT_EXTENSIONS) or mime_type.startswith("text/")

    async def extract(self, path: Path) -> list[TextSegment]:
        text = (await asyncio.to_thread(path.read_text, encoding="utf-8")).strip()
        if not text:
            return []
        return [TextSegment(text=text, source_ref=PageRef(page=1))]
