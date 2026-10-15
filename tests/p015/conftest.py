"""The p015 tests need the p015 environment (pydantic-ai and guarded-clm); elsewhere they are skipped. Every test here
runs with outbound network connections blocked, so no fake can reach a real provider by mistake."""

from __future__ import annotations

import importlib.util
import socket
from collections.abc import Iterator
from typing import Any

import pytest

if (
    importlib.util.find_spec("pydantic_ai") is None
    or importlib.util.find_spec("guarded_clm") is None
):
    collect_ignore_glob = ["test_*.py", "harness.py"]


class NetworkBlocked(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise NetworkBlocked("network access is blocked in p015 tests")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    yield
