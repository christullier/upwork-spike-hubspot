"""Shared fixtures. Every test runs with real network connections blocked."""

from __future__ import annotations

import socket

import pytest


class NetworkBlockedError(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    """Fail loudly if anything tries to open a real TCP connection.

    respx (HubSpot) and httpx2.MockTransport (Anthropic SDK) intercept requests
    before a socket is opened, so a call that reaches this guard is a test bug.
    """

    real_connect = socket.socket.connect

    def guarded_connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise NetworkBlockedError(f"real network access attempted: {address!r}")
        return real_connect(self, address)

    def guarded_create_connection(address, *args, **kwargs):
        raise NetworkBlockedError(f"real network access attempted: {address!r}")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
