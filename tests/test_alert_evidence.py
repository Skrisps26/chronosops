"""Actual HTTP probe evidence, incident identity, authentication and external recovery."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import time

from fastapi.testclient import TestClient
import pytest

import api.main as main
from tests.test_monitoring_evidence import prometheus_http, prometheus_snapshot

TOKEN = "test-alert-token-only"
ENDPOINT = "http://203.0.113.7/healthz"


@pytest.fixture
def probe_receiver(monkeypatch, prometheus_http):
    binding = {"deployment_id": "test-deployment", "service_id": "payments-primary", "tenant_id": "acme",
               "endpoint": ENDPOINT, "active": True,
               "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    graph = main._topology_graph()
    graph.query("CREATE (d:Deployment) SET d += $binding", {"binding": binding})
    monkeypatch.setenv("ALERT_WEBHOOK_TOKEN", TOKEN)
    monkeypatch.setenv("PROMETHEUS_URL", prometheus_http.url)
    prometheus_http.set_samples([prometheus_snapshot(time.time() - 1, probe_success=0, http_status=0)])
    with TestClient(main.app) as client:
        yield client, graph, prometheus_http


def payload(*, start_seconds_ago=30, state="firing", name="ChronosOpsDemoHttpDown"):
    return {"alerts": [{"status": state, "fingerprint": "same-exporter-fingerprint",
        "startsAt": (datetime.now(timezone.utc) - timedelta(seconds=start_seconds_ago)).isoformat(),
        "labels": {"alertname": name, "service": "payments-primary", "tenant_id": "acme",
                   "job": "chronosops_workload", "instance": ENDPOINT,
                   "deployment_id": "test-deployment", "probe_module": "http_2xx", "severity": "critical"}}]}


def post(client, body):
    return client.post("/internal/alerts", json=body, headers={"Authorization": "Bearer " + TOKEN})


def count_incidents(graph):
    return graph.ro_query("MATCH (i:Incident) RETURN count(i)").result_set[0][0]


def test_internal_authentication_is_required_and_has_no_default(probe_receiver, monkeypatch):
    client, graph, _ = probe_receiver
    body = payload()
    assert client.post("/internal/alerts", json=body).status_code == 401
    assert client.post("/internal/alerts", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 403
    monkeypatch.delenv("ALERT_WEBHOOK_TOKEN")
    assert post(client, body).status_code == 503
    assert count_incidents(graph) == 0


@pytest.mark.parametrize("label,value", [("instance", "http://foreign.example/healthz"),
                                         ("deployment_id", "foreign-deployment"),
                                         ("tenant_id", "globex"), ("alertname", "ArbitraryAlert")])
def test_unconfigured_alert_labels_never_create_an_incident(probe_receiver, label, value):
    client, graph, server = probe_receiver
    body = payload()
    body["alerts"][0]["labels"][label] = value
    assert post(client, body).status_code == 422
    assert count_incidents(graph) == 0
    assert server.requests == []


@pytest.mark.parametrize("snapshot", ["healthy", "stale", "exporter-down"])
def test_unconfirmed_or_unobservable_outage_cannot_create_an_incident(probe_receiver, snapshot):
    client, graph, server = probe_receiver
    if snapshot == "healthy":
        sample = prometheus_snapshot(time.time() - 1)
    elif snapshot == "stale":
        sample = prometheus_snapshot(time.time() - 60, probe_success=0, http_status=0)
    else:
        sample = prometheus_snapshot(time.time() - 1, probe_success=0, http_status=0, exporter_up=0)
    server.set_samples([sample])
    assert post(client, payload()).status_code == 503
    assert count_incidents(graph) == 0


def test_duplicate_delivery_and_later_outage_have_distinct_durable_identities(probe_receiver):
    client, graph, _ = probe_receiver
    first = payload()
    one = post(client, first)
    duplicate = post(client, deepcopy(first))
    later = post(client, payload(start_seconds_ago=15))
    assert (one.status_code, duplicate.status_code, later.status_code) == (200, 200, 200)
    first_id = one.json()["incidents"][0]["incident_id"]
    assert duplicate.json()["incidents"][0]["incident_id"] == first_id
    assert later.json()["incidents"][0]["incident_id"] != first_id
    assert count_incidents(graph) == 2
    rows = graph.ro_query("MATCH (i:Incident {incident_id:$id})-[:HEALTH_EVIDENCE]->(h:HealthObservation) "
                          "RETURN i.tenant_id, i.execution_mode, h.probe_success, h.exporter_up", {"id": first_id}).result_set
    assert rows == [["acme", "real", False, True]]


def test_resolution_without_agent_proof_is_external_not_learned_success(probe_receiver):
    client, graph, server = probe_receiver
    body = payload()
    failed = post(client, body)
    incident_id = failed.json()["incidents"][0]["incident_id"]
    server.set_samples([prometheus_snapshot(time.time() - 0.001)])
    body["alerts"][0]["status"] = "resolved"
    resolved = post(client, body)
    assert resolved.status_code == 200
    assert resolved.json()["incidents"][0]["status"] == "recovered_externally"
    assert graph.ro_query("MATCH (i:Incident {incident_id:$id}) RETURN i.status", {"id": incident_id}).result_set == [["recovered_externally"]]
    assert graph.ro_query("MATCH (a:ApprovalRecord {incident_id:$id}) RETURN count(a)", {"id": incident_id}).result_set == [[0]]


def test_monitoring_loss_is_not_an_application_outage(probe_receiver):
    client, graph, server = probe_receiver
    server.set_samples([prometheus_snapshot(time.time() - 1, exporter_up=0)])
    body = payload(name="ChronosOpsMonitoringUnknown")
    labels = body["alerts"][0]["labels"]
    for label in ("instance", "deployment_id", "probe_module"):
        labels.pop(label)
    labels["evidence_state"] = "monitoring_unknown"
    result = post(client, body)
    assert result.status_code == 200
    assert result.json()["status"] == "monitoring_unknown"
    assert result.json()["incidents"] == []
    assert count_incidents(graph) == 0


def test_monitoring_restoration_does_not_invent_a_workload_incident(probe_receiver):
    client, graph, server = probe_receiver
    server.set_samples([prometheus_snapshot(time.time() - 1)])
    result = post(client, payload(state="resolved", name="ChronosOpsMonitoringUnknown"))
    assert result.status_code == 200
    assert result.json()["status"] == "monitoring_restored"
    assert result.json()["incidents"] == []
    assert count_incidents(graph) == 0


@pytest.mark.parametrize("field,value", [("startsAt", None), ("status", []), ("alertname", [])])
def test_malformed_notification_is_validation_failure_not_server_error(probe_receiver, field, value):
    client, graph, server = probe_receiver
    body = payload()
    if field == "alertname":
        body["alerts"][0]["labels"][field] = value
    else:
        body["alerts"][0][field] = value
    assert post(client, body).status_code == 422
    assert count_incidents(graph) == 0
    assert server.requests == []
