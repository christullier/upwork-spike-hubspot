import socket

import httpx
import pytest

from tests.fakes import NetworkBlockedError


def test_real_network_is_blocked():
    with pytest.raises(NetworkBlockedError):
        socket.create_connection(("api.hubapi.com", 443), timeout=1)


def test_unmocked_http_call_fails_instead_of_leaking():
    with pytest.raises((NetworkBlockedError, httpx.ConnectError)):
        httpx.get("https://api.hubapi.com/crm/v3/objects/contacts", timeout=1)
