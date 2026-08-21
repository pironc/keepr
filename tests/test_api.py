"""End-to-end smoke test: create a conversation, attach a text file, ask a
question, and confirm the SSE stream carries ingestion status events,
streamed tokens, and a citation referencing the uploaded file — all with
MockLLMDriver/MockEmbedder, so it runs in milliseconds with no model
downloads and no real network access.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.api.routes_conversations import _media_type_for


def test_media_type_for_covers_every_ingested_extension() -> None:
    # .md gets its own type (the frontend renders it through the markdown
    # renderer); every other TextIngestor extension previews the same way,
    # as plain text — that's the whole point of reusing _TEXT_EXTENSIONS
    # instead of hand-listing extensions here too.
    assert _media_type_for("report.pdf") == "application/pdf"
    assert _media_type_for("notes.md") == "text/markdown"
    assert _media_type_for("notes.txt") == "text/plain"
    assert _media_type_for("script.py") == "text/plain"
    assert _media_type_for("data.csv") == "text/plain"
    # Not ingestible by anything today (see AudioVideoIngestor) — previewing
    # it is out of scope, and it must not silently collapse to text/plain.
    assert _media_type_for("archive.zip") == "application/octet-stream"


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "startup" in data


def test_full_flow_ingest_and_ask(client: TestClient) -> None:
    conversation = client.post("/conversations").json()
    conversation_id = conversation["id"]

    files = {"files": ("manual.txt", b"The drone's max flight time is 28 minutes.", "text/plain")}
    data = {"prompt": "How long can the drone fly?"}

    response = client.post(f"/conversations/{conversation_id}/messages", data=data, files=files)
    assert response.status_code == 200

    body = response.text
    assert "event: document_status" in body
    assert '"status": "indexed"' in body
    assert "event: token" in body
    assert "event: citations" in body
    assert "manual.txt" in body

    documents = client.get(f"/conversations/{conversation_id}/documents").json()
    assert len(documents) == 1
    assert documents[0]["status"] == "indexed"

    messages = client.get(f"/conversations/{conversation_id}/messages").json()
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"
    assert len(messages[1]["citations"]) == 1
    assert messages[1]["citations"][0]["document_filename"] == "manual.txt"


def test_unsupported_file_type_reports_clearly_not_silently(client: TestClient) -> None:
    conversation = client.post("/conversations").json()
    conversation_id = conversation["id"]

    files = {"files": ("clip.mp4", b"not a real video", "video/mp4")}
    data = {"prompt": "Summarize this clip"}

    response = client.post(f"/conversations/{conversation_id}/messages", data=data, files=files)

    assert '"status": "unsupported"' in response.text

    documents = client.get(f"/conversations/{conversation_id}/documents").json()
    assert documents[0]["status"] == "unsupported"
    assert documents[0]["error_message"] is not None


def test_upload_filename_is_sanitized_to_bare_basename(
    client: TestClient,
) -> None:
    """A crafted upload filename must not escape the upload dir or the
    Content-Disposition header.

    Filenames are client-supplied; without sanitizing, a name like
    ``../../evil.txt`` resolves outside the upload directory on disk (and
    a name containing quotes/CR/LF can inject bytes into the
    ``Content-Disposition`` header on the download route). Both the
    persisted document row and the served download must use the bare,
    stripped basename.
    """
    conversation_id = client.post("/conversations").json()["id"]

    # A path-traversal name and a header-injection name — distinct content so
    # the pipeline's content-hash dedup doesn't collapse them into one file.
    hostile = [
        ("../../evil.txt", b"first distinctive payload text"),
        ('a"b\r\nX-Injected: yes\n.txt', b"second distinctive payload text"),
    ]
    for name, content in hostile:
        files = {"files": (name, content, "text/plain")}
        data = {"prompt": "Summarize this file"}
        response = client.post(
            f"/conversations/{conversation_id}/messages", data=data, files=files
        )
        assert response.status_code == 200

    documents = client.get(f"/conversations/{conversation_id}/documents").json()
    assert len(documents) == 2

    # The traversal name must reduce to a bare basename (no `..`/separators
    # that could escape the upload dir). The CR/LF+quote name's literal control
    # characters are either percent-encoded by the multipart encoder before we
    # see them, or stripped by the sanitizer — either way the invariant that
    # matters is that nothing that could frame a header line or a path
    # traversal survives on disk or in the download response.
    saved = {d["filename"] for d in documents}
    assert any(not name.startswith("..") and "/" not in name for name in saved)
    assert "evil.txt" in saved

    # No stray bare "upload" fallback appeared (both hostile names had a
    # non-empty basename that survived).
    assert "upload" not in saved

    # Downloading each file must serve with a clean Content-Disposition
    # header — no literal CR/LF or quote that could smuggle a new header line.
    for document in documents:
        sid = document["id"]
        path = f"/conversations/{conversation_id}/documents/{sid}/file"
        down = client.get(path)
        assert down.status_code == 200
        disposition = down.headers.get("content-disposition", "")
        assert "\r" not in disposition and "\n" not in disposition
        assert '"' in disposition  # still a properly quoted filename


def test_pinning_round_trips_and_sorts_pinned_first(
    client: TestClient,
) -> None:
    """The pin/PATCH flow must persist and be returned by the API: PATCHing
    ``pinned: true`` makes GET return ``pinned: true`` (so the sidebar renders
    the pin icon + "Unpin"), and the list orders pinned chats before unpinned
    ones regardless of recency.  This guards the regression where pinning a
    chat moved it to the top of the list but the UI showed no pin state and
    offered no way to unpin (the sidebar only shows the pushpin/”Unpin” when
    the API actually reports ``pinned: true``)."""
    # Two brand-new conversations, neither pinned.
    a = client.post("/conversations").json()
    b = client.post("/conversations").json()
    assert a["pinned"] is False
    assert b["pinned"] is False

    # Pin the FIRST one (created before the second, so among unpinned the
    # second would sort first by recency if pinning were a no-op).
    patch = client.patch(
        f"/conversations/{a['id']}",
        json={"pinned": True},
    )
    assert patch.status_code == 200
    assert patch.json()["pinned"] is True

    # The read path reports the persisted pin state — what the sidebar render
    # depends on.
    got = client.get(f"/conversations/{a['id']}").json()
    assert got["pinned"] is True

    # Pinned first in the listing, regardless of recency.
    listing = client.get("/conversations").json()
    assert listing[0]["id"] == a["id"]
    assert all(not c["pinned"] for c in listing[1:])

    # Unpinning restores recency ordering and clears the flag.
    patch = client.patch(
        f"/conversations/{a['id']}",
        json={"pinned": False},
    )
    assert patch.status_code == 200
    assert patch.json()["pinned"] is False
    listing = client.get("/conversations").json()
    assert listing[0]["id"] == b["id"]
