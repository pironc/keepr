"""Tests for the backend binary's fast pre-bind startup check.

``backend_main.port_in_use`` probes whether the HTTP bind address is already
claimed — the Tauri shell relies on the backend failing fast and exiting so it
can tell the user immediately ("a previous instance is still running") instead
of both waiting out the full 30-second health time-out.

These tests run under pytest's global ``--disable-socket`` (no real network
sockets at all — see conftest.py), so ``socket.socket`` is monkeypatched with a
fake that simulates a bind success or failure, proving the branch logic without
opening a single real socket.
"""

from __future__ import annotations

import socket
from typing import Any

import backend_main as _bm
import pytest


class _FakeSocket:
    """Minimal stand-in for the socket the real helper opens, driven by an
    injected ``bind`` behaviour."""

    def __init__(self, bind_behaviour: Any) -> None:
        self._bind_behaviour = bind_behaviour
        self.closed = False

    def setsockopt(self, *_: Any) -> None:
        return None

    def bind(self, _addr: Any) -> None:
        raise_behaviour = self._bind_behaviour
        if raise_behaviour is not None:
            raise raise_behaviour

    def close(self) -> None:
        self.closed = True


def _monkeypatch_socket(monkeypatch: pytest.MonkeyPatch, bind_behaviour: Any) -> None:
    def fake_socket(_family: int, _kind: int) -> _FakeSocket:
        return _FakeSocket(bind_behaviour)

    # Patch the real ``socket`` module's ``socket`` attribute. backend_main's
    # helper holds a reference to that same module object (``import socket``),
    # so this reaches it without depending on backend_main re-exporting
    # anything — and keeps mypy-strict happy (patching ``backend_main.socket``
    # tripped its not-exported check).
    monkeypatch.setattr(socket, "socket", fake_socket)


def test_port_in_use_reports_free_when_bind_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _monkeypatch_socket(monkeypatch, bind_behaviour=None)
    assert _bm.port_in_use("127.0.0.1", 8000) is False


def test_port_in_use_reports_occupied_when_bind_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _monkeypatch_socket(monkeypatch, bind_behaviour=OSError("address already in use"))
    assert _bm.port_in_use("127.0.0.1", 8000) is True


def test_port_in_use_closes_probe_socket_even_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[_FakeSocket] = []

    def fake_socket(_family: int, _kind: int) -> _FakeSocket:
        fs = _FakeSocket(None)
        created.append(fs)
        return fs

    monkeypatch.setattr(socket, "socket", fake_socket)
    _bm.port_in_use("127.0.0.1", 8000)
    assert len(created) == 1
    assert created[0].closed is True
