"""Focused API route tests for authenticated simulation workflows."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import api.main as main
from api.contracts import get_tenant_id
from agents.actions import ActionConflictError, ActionNotFoundError


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    """Use an explicit trusted-identity override for non-boundary API tests."""
    monkeypatch.setenv("CHRONOSOPS_PROXY_TOKEN", "test-proxy-token-only")
    main.app.dependency_overrides[get_tenant_id] = lambda: "acme"
    try:
        with TestClient(main.app) as test_client:
            yield test_client
    finally:
        main.app.dependency_overrides.clear()


def test_health_readiness_and_shared_topology(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}

    readiness = client.get("/readyz")
    assert readiness.status_code == 200
    assert readiness.json()["ready"] is True

    topology = client.get("/api/topology")
    assert topology.status_code == 200
    data = topology.json()
    assert data["node_count"] > 0
    assert data["edge_count"] > 0


def test_simulation_creates_tenant_owned_incident_and_run(client: TestClient) -> None:
    created = client.post(
        "/api/incidents",
        json={
            "execution_mode": "simulation",
            "service_id": "payments-primary",
            "max_cost": 12,
            "session_id": "api-test-session",
        },
    )
    assert created.status_code == 200
    result = created.json()

    ownership = main._topology_graph().ro_query(
        "MATCH (s:Session {tenant_id: 'acme', session_id: 'api-test-session'}) "
        "-[:HAS_RUN]->(r:Run {run_id: $run_id, tenant_id: 'acme'}) "
        "-[:FOR_INCIDENT]->(i:Incident {incident_id: $incident_id, tenant_id: 'acme'}) "
        "RETURN s.session_id",
        {"run_id": result["run_id"], "incident_id": result["incident_id"]},
    ).result_set
    assert ownership == [["api-test-session"]]
    assert result["incident_id"].startswith("inc-sim-")
    assert result["run_id"].startswith("run-")

    detail = client.get(f"/api/incidents/{result['incident_id']}")
    assert detail.status_code == 200
    assert detail.json()["incident_id"] == result["incident_id"]

    listed = client.get("/api/incidents")
    assert listed.status_code == 200
    assert result["incident_id"] in {item["incident_id"] for item in listed.json()["incidents"]}


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [(ActionNotFoundError("not found"), 404), (ActionConflictError("conflict"), 409)],
)
def test_rejection_errors_have_domain_http_statuses(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_status: int,
) -> None:
    async def fail_rejection(**_: object) -> dict[str, object]:
        raise error

    monkeypatch.setattr(main, "reject_action", fail_rejection)
    response = client.post(
        "/api/incidents/inc-test/reject",
        json={"approval_id": "appr-test"},
    )
    assert response.status_code == expected_status


def test_untrusted_browser_origin_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/incidents",
        json={"service_id": "payments-primary"},
        headers={"Origin": "https://untrusted.example"},
    )
    assert response.status_code == 403
