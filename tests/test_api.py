"""End-to-end smoke test: create a conversation, attach a text file, ask a
question, and confirm the SSE stream carries ingestion status events,
streamed tokens, and a citation referencing the uploaded file — all with
MockLLMDriver/MockEmbedder, so it runs in milliseconds with no model
downloads and no real network access.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("LLM_DRIVER", "mock")
    monkeypatch.setenv("EMBEDDER", "mock")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "keepr.db"))
    monkeypatch.setenv("INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("RETRIEVAL_MIN_SIMILARITY", "0.0")

    from src.api.app import app

    with TestClient(app) as test_client:
        yield test_client


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
