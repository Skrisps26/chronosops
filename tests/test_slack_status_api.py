"""Authenticated HTTP observation of actual persisted Slack connector state."""
from __future__ import annotations

import os
import time

from fastapi.testclient import TestClient

import api.main as main
from seed.sources import _graph


def _headers(tenant: str) -> dict[str, str]:
    return {"X-Tenant-ID": tenant,
            "X-ChronosOps-Proxy-Token": os.environ["CHRONOSOPS_PROXY_TOKEN"]}


def test_connector_heartbeat_and_authenticated_owner_are_observed(monkeypatch) -> None:
    monkeypatch.setenv("SLACK_STATUS_STALE_SECONDS", "10")
    graph = _graph()
    now = time.time()
    graph.query(
        "MERGE (c:SlackConnector {tenant_id:'acme',connector_id:'slack'}) "
        "SET c.connected=true,c.connected_at=$now,c.heartbeat_at=$now,"
        "c.last_event_at=$now,c.reconciliation_gap=true",
        {"now": now}, timeout=3000,
    )
    with TestClient(main.app) as client:
        assert client.get("/api/connectors/slack", headers={"X-Tenant-ID": "acme"}).status_code == 401
        current = client.get("/api/connectors/slack", headers=_headers("acme"))
        assert current.status_code == 200
        assert current.json()["status"] == "connected"
        assert current.json()["available"] is True
        assert current.json()["reconciliation_gap"] is True
        graph.query("MATCH (c:SlackConnector {tenant_id:'acme',connector_id:'slack'}) "
                    "SET c.heartbeat_at=$old", {"old": now - 600}, timeout=3000)
        stale = client.get("/api/connectors/slack", headers=_headers("acme"))
        assert stale.json()["status"] == "stale"
        assert stale.json()["available"] is False
        foreign = client.get("/api/connectors/slack?tenant_id=acme", headers=_headers("globex"))
        assert foreign.status_code == 200
        assert foreign.json()["tenant_id"] == "globex"
        assert foreign.json()["status"] == "not_owner"
        assert foreign.json()["available"] is False
        assert not {"connected_at", "heartbeat_at", "last_event_at"}.intersection(foreign.json())


def test_connector_status_never_falls_back_to_admin(monkeypatch) -> None:
    _graph().query("MERGE (c:SlackConnector {tenant_id:'acme',connector_id:'slack'}) "
                   "SET c.connected=true,c.heartbeat_at=$now", {"now": time.time()}, timeout=3000)
    monkeypatch.setenv("FALKORDB_READER_ACME_PASSWORD", "")
    with TestClient(main.app) as client:
        response = client.get("/api/connectors/slack", headers=_headers("acme"))
        assert response.status_code == 200
        assert response.json()["available"] is False
        assert response.json()["status"] == "unavailable"
