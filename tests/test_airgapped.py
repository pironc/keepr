"""Proves the air-gapped guard is actually active.

Every test in this suite already implicitly proves "no network calls" by
simply passing (`--disable-socket` in pyproject.toml would fail any test
that opened a real socket) — this is the explicit positive control: the
one test that would fail if that protection were ever accidentally
disabled or misconfigured.
"""

from __future__ import annotations

import socket

import pytest
from pytest_socket import SocketBlockedError


def test_real_network_sockets_are_blocked() -> None:
    with pytest.raises(SocketBlockedError):
        socket.create_connection(("1.1.1.1", 80), timeout=1)
