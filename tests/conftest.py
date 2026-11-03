"""Shared fixtures. Every test runs with real network connections blocked."""

from __future__ import annotations

import socket

import pytest
import respx
from fastapi.testclient import TestClient

from app.classify import Classifier
from app.config import Settings
from app.hubspot import HubSpotClient
from app.idempotency import Ledger
from app.main import create_app
from tests.fakes import (
    HUBSPOT_BASE,
    SECRET,
    STAGES,
    FakeClaude,
    HubSpotRoutes,
    NetworkBlockedError,
)


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


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        webhook_secret=SECRET,
        hubspot_token="test-hubspot-token",
        pipeline_id="intake-pipeline",
        stage_ids=dict(STAGES),
        owner_ids={"coi": "owner-coi-desk"},
        anthropic_model="claude-test",
        hubspot_base_url=HUBSPOT_BASE,
        idempotency_db_path=str(tmp_path / "ledger.db"),
    )


@pytest.fixture
def claude() -> FakeClaude:
    return FakeClaude()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def hubspot_mock():
    with respx.mock(base_url=HUBSPOT_BASE, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def client(settings, claude, sleeps, hubspot_mock):
    hubspot = HubSpotClient(
        settings.hubspot_token, settings.hubspot_base_url, max_retries=3, sleep=sleeps.append
    )
    app = create_app(
        settings,
        classifier=Classifier(claude.client(), settings.anthropic_model),
        hubspot=hubspot,
        ledger=Ledger(settings.idempotency_db_path),
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def hubspot(hubspot_mock) -> HubSpotRoutes:
    return HubSpotRoutes(hubspot_mock)
