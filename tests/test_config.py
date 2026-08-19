"""`_default_driver`'s packaged-vs-dev distinction: a packaged (PyInstaller)
build must never silently resolve to mock, even with no model downloaded
yet or `llama_cpp` not importable — an end user with no model installed
must see the real "no model installed" refusal (RagEngine's availability
gate / ModelUnavailableError), not a meaningless mock-generated answer.
Dev/CI (KEEPR_FROZEN unset) keeps today's friction-free mock fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import _default_driver


def test_frozen_defaults_to_llama_cpp_even_with_no_model_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEEPR_FROZEN", "1")
    assert _default_driver(tmp_path / "nope.gguf") == "llama_cpp"


def test_frozen_defaults_to_llama_cpp_regardless_of_llama_cpp_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the bundled backend somehow lacks the llama_cpp package, the
    packaged app must still select llama_cpp — never silently fall back to
    mock — so the failure surfaces as a clean ModelUnavailableError instead
    of a mock-generated non-answer."""
    monkeypatch.setenv("KEEPR_FROZEN", "1")
    model_path = tmp_path / "model.gguf"
    model_path.write_bytes(b"not a real gguf")
    assert _default_driver(model_path) == "llama_cpp"


def test_unfrozen_still_defaults_to_mock_with_no_model_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KEEPR_FROZEN", raising=False)
    assert _default_driver(tmp_path / "nope.gguf") == "mock"


def test_unfrozen_defaults_to_mock_when_llama_cpp_package_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KEEPR_FROZEN", raising=False)
    import sys

    monkeypatch.setitem(sys.modules, "llama_cpp", None)
    model_path = tmp_path / "model.gguf"
    model_path.write_bytes(b"not a real gguf")
    assert _default_driver(model_path) == "mock"
