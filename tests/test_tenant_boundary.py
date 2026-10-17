"""Proxy-bound tenant authorization tests without dependency overrides."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from falkordb import FalkorDB
from starlette.websockets import WebSocketDisconnect

import api.main as main
from agents.settings import graph_names
from api.contracts import EventEnvelope, manager
from seed.load import get_config

PROXY_TOKEN = "test-proxy-token-only"
PUBLIC_ORIGIN = "https://console.chronosops.test"


def _headers(tenant: str, *, origin: str | None = None) -> dict[str, str]:
    headers = {
        "X-Tenant-ID": tenant,
        "X-ChronosOps-Proxy-Token": PROXY_TOKEN,
    }
    if origin is not None:
        headers["Origin"] = origin
    return headers


def _topology_graph():
    config = get_config()
    db = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    )
    return db.select_graph(graph_names().topology)


def _seed_acme_run_and_incident() -> tuple[str, str, str]:
    run_id = "run-acme-boundary"
    incident_id = "inc-acme-boundary"
    approval_id = "appr-acme-boundary"
    created_at = datetime.now(timezone.utc).isoformat()
    _topology_graph().query(
        "MERGE (i:Incident {incident_id: $incident_id, tenant_id: 'acme'}) "
        "SET i.service_id = 'payments-primary', i.status = 'awaiting_approval', "
        "    i.starts_at = $created_at "
        "MERGE (r:Run {run_id: $run_id, tenant_id: 'acme'}) "
        "SET r.incident_id = $incident_id, r.created_at = $created_at "
        "MERGE (r)-[:FOR_INCIDENT]->(i) "
        "MERGE (a:ApprovalRecord {approval_id: $approval_id}) "
        "SET a.incident_id = $incident_id, a.tenant_id = 'acme', "
        "    a.execution_mode = 'simulation', a.action_kind = 'simulate_failover', "
        "    a.proposed_action = '{\"route\": []}', a.status = 'pending', "
        "    a.created_at = $created_at, a.expires_at = '2099-01-01T00:00:00+00:00'",
        {
            "run_id": run_id,
            "incident_id": incident_id,
            "approval_id": approval_id,
            "created_at": created_at,
        },
    )
    return run_id, incident_id, approval_id


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHRONOSOPS_PROXY_TOKEN", PROXY_TOKEN)
    monkeypatch.setenv("PUBLIC_ORIGIN", PUBLIC_ORIGIN)
    main.app.dependency_overrides.clear()
    manager.active_connections.clear()
    manager.event_history.clear()
    try:
        with TestClient(main.app) as test_client:
            yield test_client
    finally:
        main.app.dependency_overrides.clear()
        manager.active_connections.clear()
        manager.event_history.clear()


@pytest.mark.parametrize(
    "path",
    [
        "/api/topology",
        "/api/monitoring",
        "/api/incidents",
        "/api/sources/rfc-001-payments-failover",
    ],
)
def test_user_data_routes_require_proxy_identity(client: TestClient, path: str) -> None:
    response = client.get(path, headers={"X-Tenant-ID": "acme"})
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/questions", {"question": "Who owns payments-primary?"}),
        ("/api/incidents", {"service_id": "payments-primary"}),
        ("/api/incidents/inc-missing/approve", {"approval_id": "appr-missing"}),
        ("/api/incidents/inc-missing/reject", {"approval_id": "appr-missing"}),
    ],
)
def test_user_write_routes_require_proxy_identity(
    client: TestClient, path: str, payload: dict[str, str]
) -> None:
    response = client.post(path, json=payload, headers={"X-Tenant-ID": "acme"})
    assert response.status_code == 401


def test_forged_or_unknown_proxy_identity_is_denied(client: TestClient) -> None:
    forged_consumer = client.get(
        "/api/topology",
        headers={
            "X-Consumer-Custom-ID": "acme",
            "X-ChronosOps-Proxy-Token": PROXY_TOKEN,
        },
    )
    assert forged_consumer.status_code == 401

    unknown_tenant = client.get("/api/topology", headers=_headers("other"))
    assert unknown_tenant.status_code == 401

    invalid_token = client.get(
        "/api/topology",
        headers={"X-Tenant-ID": "acme", "X-ChronosOps-Proxy-Token": "forged-token"},
    )
    assert invalid_token.status_code == 401



def test_shared_topology_is_authorized_for_both_tenants(client: TestClient) -> None:
    acme = client.get("/api/topology", headers=_headers("acme"))
    globex = client.get("/api/topology", headers=_headers("globex"))

    assert acme.status_code == 200
    assert globex.status_code == 200
    assert acme.json()["node_count"] == globex.json()["node_count"]


def test_globex_cannot_read_list_or_operate_on_acme_incident(client: TestClient) -> None:
    _, incident_id, approval_id = _seed_acme_run_and_incident()

    listed = client.get("/api/incidents", headers=_headers("globex"))
    assert listed.status_code == 200
    assert incident_id not in {item["incident_id"] for item in listed.json()["incidents"]}

    detail = client.get(f"/api/incidents/{incident_id}", headers=_headers("globex"))
    assert detail.status_code == 404

    approve = client.post(
        f"/api/incidents/{incident_id}/approve",
        json={"approval_id": approval_id},
        headers=_headers("globex"),
    )
    assert approve.status_code == 404

    reject = client.post(
        f"/api/incidents/{incident_id}/reject",
        json={"approval_id": approval_id},
        headers=_headers("globex"),
    )
    assert reject.status_code == 404


def test_globex_cannot_read_acme_monitoring_evidence(client: TestClient) -> None:
    response = client.get("/api/monitoring", headers=_headers("globex"))
    assert response.status_code == 404
    assert "target" not in response.json()


def test_socket_receives_owned_event_and_denies_foreign_or_wrong_origin(client: TestClient) -> None:
    run_id, _, _ = _seed_acme_run_and_incident()
    asyncio.run(
        manager.broadcast(
            run_id,
            EventEnvelope(
                run_id=run_id,
                sequence=1,
                type="run_started",
                payload={"incident_id": "inc-acme-boundary"},
            ),
        )
    )

    with client.websocket_connect(
        f"/api/runs/{run_id}/events",
        headers=_headers("acme", origin=PUBLIC_ORIGIN),
    ) as websocket:
        event = json.loads(websocket.receive_text())
    assert event["run_id"] == run_id
    assert event["type"] == "run_started"

    with pytest.raises(WebSocketDisconnect) as foreign:
        with client.websocket_connect(
            f"/api/runs/{run_id}/events",
            headers=_headers("globex", origin=PUBLIC_ORIGIN),
        ):
            pass
    assert foreign.value.code == 1008
    assert not manager.active_connections.get(run_id)


    with pytest.raises(WebSocketDisconnect) as unknown_run:
        with client.websocket_connect(
            "/api/runs/run-unknown-boundary/events",
            headers=_headers("acme", origin=PUBLIC_ORIGIN),
        ):
            pass
    assert unknown_run.value.code == 1008

    with pytest.raises(WebSocketDisconnect) as missing_origin:
        with client.websocket_connect(
            f"/api/runs/{run_id}/events",
            headers=_headers("acme"),
        ):
            pass
    assert missing_origin.value.code == 1008

    with pytest.raises(WebSocketDisconnect) as invalid_origin:
        with client.websocket_connect(
            f"/api/runs/{run_id}/events",
            headers=_headers("acme", origin="https://arbitrary.trycloudflare.com"),
        ):
            pass
    assert invalid_origin.value.code == 1008


def test_readiness_is_unready_without_proxy_configuration(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CHRONOSOPS_PROXY_TOKEN")
    response = client.get("/readyz")
    assert response.status_code == 503


