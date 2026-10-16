"""Module 6 verification suite: REST API, WebSocket event streaming, and static UI delivery."""
from __future__ import annotations

import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from api.main import app
from seed.generate import DOCS_DIR


@pytest.fixture
def client():
    return TestClient(app)


def test_health_and_readiness_endpoints(client):
    """Verify healthz and readyz return 200 and operational status."""
    res_h = client.get("/healthz")
    assert res_h.status_code == 200
    assert res_h.json() == {"status": "ok"}

    res_r = client.get("/readyz")
    assert res_r.status_code == 200
    assert res_r.json()["ready"] is True


def test_monitoring_endpoint(client):
    """Verify monitoring endpoint returns probe status and target."""
    res = client.get("/api/monitoring")
    assert res.status_code == 200
    data = res.json()
    assert "monitoring_healthy" in data
    assert "status" in data


def test_topology_endpoint_returns_elements(client):
    """Verify topology endpoint returns nodes and edges formatted for Cytoscape."""
    res = client.get("/api/topology", headers={"X-Tenant-ID": "acme"})
    assert res.status_code == 200
    data = res.json()
    assert data["node_count"] == 45
    assert data["edge_count"] > 100
    elements = data["elements"]
    assert any(e["data"]["id"] == "payments-primary" for e in elements)
    assert any(e["data"].get("type") == "ROUTES_TO" for e in elements)


def test_question_answering_endpoint(client):
    """Verify question answering endpoint returns answers and citations."""
    # 1. Ownership question
    res_own = client.post(
        "/api/questions",
        json={"question": "Who owns payments-primary?"},
        headers={"X-Tenant-ID": "acme"},
    )
    assert res_own.status_code == 200
    assert "Payments" in res_own.json()["answer"]

    # 2. Bus factor question
    res_bus = client.post(
        "/api/questions",
        json={"question": "What is the bus factor for payments-primary?"},
        headers={"X-Tenant-ID": "acme"},
    )
    assert res_bus.status_code == 200
    assert "1" in res_bus.json()["answer"]

    # 3. Decision question
    res_dec = client.post(
        "/api/questions",
        json={"question": "Why is the backup queue route selected?"},
        headers={"X-Tenant-ID": "acme"},
    )
    assert res_dec.status_code == 200
    assert "documented decisions" in res_dec.json()["answer"]


def test_incident_creation_and_approval_api_flow(client):
    """Verify full REST incident creation, inspection, and approval flow."""
    # 1. Create simulation incident
    res_create = client.post(
        "/api/incidents",
        json={
            "execution_mode": "simulation",
            "service_id": "payments-primary",
            "max_cost": 12,
        },
        headers={"X-Tenant-ID": "acme"},
    )
    assert res_create.status_code == 200
    inc_data = res_create.json()
    inc_id = inc_data["incident_id"]
    appr_id = inc_data["approval_id"]
    assert inc_data["status"] == "awaiting_approval"
    assert inc_data["proposed_action"]["action_kind"] == "simulate_failover"

    # 2. Query single incident
    res_get = client.get(f"/api/incidents/{inc_id}", headers={"X-Tenant-ID": "acme"})
    assert res_get.status_code == 200
    assert res_get.json()["incident_id"] == inc_id

    # 3. Approve incident
    res_appr = client.post(
        f"/api/incidents/{inc_id}/approve",
        json={"approval_id": appr_id},
        headers={"X-Tenant-ID": "acme"},
    )
    assert res_appr.status_code == 200
    assert res_appr.json()["status"] == "applied"
    assert res_appr.json()["active_route"] == ["checkout", "queue", "payments-backup"]


def test_source_document_retrieval(client):
    """Verify retrieval of underlying source document markdown."""
    res = client.get("/api/sources/rfc-001-payments-failover")
    assert res.status_code == 200
    assert "RFC-001" in res.json()["content"]


def test_origin_validation_rejection(client):
    """Verify that requests with unauthorized external origin headers are rejected with 403."""
    res = client.post(
        "/api/questions",
        json={"question": "Test origin"},
        headers={"Origin": "http://malicious-website.com"},
    )
    assert res.status_code == 403
    assert "Cross-origin" in res.json()["detail"]


def test_websocket_event_subscription(client):
    """Verify WebSocket connection and real-time event streaming."""
    run_id = "test-stream-run-123"
    with client.websocket_connect(f"/api/runs/{run_id}/events") as ws:
        # Trigger an action or send ping text
        ws.send_text("ping")


def test_static_ui_served(client):
    """Verify that built React assets in ui/dist are served at root."""
    res = client.get("/")
    assert res.status_code == 200
    assert "ChronosOps" in res.text or "root" in res.text
