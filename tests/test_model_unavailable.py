"""Model-unavailable containment: a missing or corrupt GGUF file must surface
as a :class:`ModelUnavailableError` from both the llama.cpp LLM driver and the
embedder — never a raw llama-cpp exception leaking out of `_load()`.

llama-cpp-python is an optional, heavier extra (`.[llama]`) not installed in
the default dev/CI environment, so none of this may depend on it actually
being importable. The missing-file branch never needs it at all: `_load()`
checks the file exists before its lazy `from llama_cpp import Llama`, so that
branch raises before the import is ever reached. The corrupt-file branch does
need a `llama_cpp.Llama` to patch — `fake_llama_cpp_module` below injects a
throwaway module into `sys.modules` for the test's duration so that works
whether or not the real package is present.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from src.embeddings.llama_cpp_embedder import LlamaCppEmbedder
from src.llm.llama_cpp_driver import LlamaCppDriver
from src.model_unavailable import ModelUnavailableError
from src.models import LLMMessage


class _Boom:
    """Stands in for `llama_cpp.Llama`: the constructor raises, mimicking a
    truncated/corrupt/unsupported GGUF failing to load."""

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("GGML_ASSERT: failed to parse tensor data")


@pytest.fixture
def missing_path(tmp_path: Path) -> Path:
    p = tmp_path / "nope.gguf"
    assert not p.exists()
    return p


@pytest.fixture
def corrupt_path(tmp_path: Path) -> Path:
    p = tmp_path / "truncated.gguf"
    p.write_bytes(b"\x00\x00not a real gguf")  # present on disk, but unloadable
    return p


@pytest.fixture
def fake_llama_cpp_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Stand in for the real `llama_cpp` package for the test's duration, so
    `from llama_cpp import Llama` inside `_load()` resolves to `_Boom`
    regardless of whether llama-cpp-python is actually installed."""
    fake = types.ModuleType("llama_cpp")
    fake.Llama = _Boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llama_cpp", fake)
    return fake


def test_driver_missing_file_raises_model_unavailable(missing_path: Path) -> None:
    driver = LlamaCppDriver(missing_path, n_ctx=2048, n_gpu_layers=0)
    with pytest.raises(ModelUnavailableError, match="Language model file not found"):
        driver._load()


def test_embedder_missing_file_raises_model_unavailable(missing_path: Path) -> None:
    embedder = LlamaCppEmbedder(missing_path, n_gpu_layers=0)
    with pytest.raises(ModelUnavailableError, match="Embedding model file not found"):
        embedder._load()


def test_driver_corrupt_file_wraps_load_error_as_model_unavailable(
    corrupt_path: Path, fake_llama_cpp_module: types.ModuleType
) -> None:
    driver = LlamaCppDriver(corrupt_path, n_ctx=2048, n_gpu_layers=0)
    with pytest.raises(ModelUnavailableError) as excinfo:
        driver._load()
    assert "could not be loaded" in str(excinfo.value)
    # The original llama.cpp failure stays attached for diagnostics.
    assert isinstance(excinfo.value.__cause__, RuntimeError)


def test_embedder_corrupt_file_wraps_load_error_as_model_unavailable(
    corrupt_path: Path, fake_llama_cpp_module: types.ModuleType
) -> None:
    embedder = LlamaCppEmbedder(corrupt_path, n_gpu_layers=0)
    with pytest.raises(ModelUnavailableError) as excinfo:
        embedder._load()
    assert "could not be loaded" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, RuntimeError)


def test_driver_carry_role_language() -> None:
    """A language-model failure is tagged with the LANGUAGE role so the RAG
    engine can phrase the message for the right model."""
    import asyncio

    from src.model_unavailable import ModelRole

    driver = LlamaCppDriver(Path("/nonexistent/qwen.gguf"), n_ctx=2048, n_gpu_layers=0)

    async def capture() -> None:
        try:
            async for _ in driver.generate([LLMMessage(role="user", content="hi")]):
                pass
        except ModelUnavailableError as exc:
            assert exc.role == ModelRole.LANGUAGE
            assert "Language model file not found" in str(exc)
            return
        raise AssertionError("driver.generate must raise ModelUnavailableError")

    asyncio.run(capture())


def test_embedder_carry_role_embedding() -> None:
    """An embedding-model failure is tagged with the EMBEDDING role."""
    from src.model_unavailable import ModelRole

    embedder = LlamaCppEmbedder(Path("/nonexistent/nomic.gguf"), n_gpu_layers=0)
    try:
        embedder._load()
    except ModelUnavailableError as exc:
        assert exc.role == ModelRole.EMBEDDING
        assert "Embedding model file not found" in str(exc)
        return
    raise AssertionError("embedder._load must raise ModelUnavailableError")


def test_driver_availability_reports_missing_file(missing_path: Path) -> None:
    """availability() cheaply reports a missing language-model file without
    attempting a load — the signal the RAG engine uses to detect "both models
    missing" when retrieval fails first."""
    import asyncio

    driver = LlamaCppDriver(missing_path, n_ctx=2048, n_gpu_layers=0)
    reason = asyncio.run(driver.availability())
    assert reason is not None
    assert "Language model file not found" in reason


def test_embedder_availability_reports_missing_file(missing_path: Path) -> None:
    import asyncio

    embedder = LlamaCppEmbedder(missing_path, n_gpu_layers=0)
    reason = asyncio.run(embedder.availability())
    assert reason is not None
    assert "Embedding model file not found" in reason


def test_mock_models_report_available() -> None:
    """Mocks always report available (None) — they need no model file."""
    import asyncio

    from src.embeddings.mock_embedder import MockEmbedder
    from src.llm.mock_driver import MockLLMDriver

    assert asyncio.run(MockEmbedder().availability()) is None
    assert asyncio.run(MockLLMDriver().availability()) is None
