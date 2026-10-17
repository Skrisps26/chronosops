"""Cross-check internal Alertmanager notifications against the one bound probe target."""
from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from falkordb import FalkorDB

from agents.settings import graph_names
from integrations.deployment import bound_deployment
from integrations.health_store import persist_health_samples
from integrations.monitoring import MonitoringClient
from seed.load import get_config


class AlertEvidenceUnavailable(ValueError):
    """The notification has no current attributable monitoring evidence."""


def _normalize(alert: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
    labels = alert.get("labels", {})
    expected = {"service": binding["service_id"], "tenant_id": "acme", "job": "chronosops_workload",
                "instance": binding["endpoint"], "deployment_id": binding["deployment_id"], "probe_module": "http_2xx"}
    name = labels.get("alertname") if isinstance(labels, dict) else None
    required = expected if name != "ChronosOpsMonitoringUnknown" else {
        "service": binding["service_id"], "tenant_id": "acme", "job": "chronosops_workload"
    }
    if not isinstance(labels, dict) or any(labels.get(key) != value for key, value in required.items()):
        raise ValueError("Alert labels do not identify the verified workload")
    if any(key in labels and labels[key] != value for key, value in expected.items()):
        raise ValueError("Alert identifies a foreign deployment")
    if set(labels) - (expected.keys() | {"alertname", "severity", "evidence_state"}):
        raise ValueError("Unexpected alert labels")
    if name not in {"ChronosOpsDemoHttpDown", "ChronosOpsMonitoringUnknown"}:
        raise ValueError("Alert is not allowlisted")
    state = alert.get("status")
    if state not in {"firing", "resolved"}:
        raise ValueError("Invalid alert state")
    fingerprint = alert.get("fingerprint", "")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", fingerprint):
        raise ValueError("Missing or invalid alert fingerprint")
    try:
        started = datetime.fromisoformat(alert["startsAt"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Missing or invalid alert start time") from error
    if started.tzinfo is None or started > datetime.now(timezone.utc):
        raise ValueError("Alert start time must be an observed timezone-aware timestamp")
    starts_at = started.astimezone(timezone.utc).isoformat()
    identity = f"{binding['deployment_id']}:{fingerprint}:{starts_at}"
    return {"incident_id": "inc-" + hashlib.sha256(identity.encode()).hexdigest()[:32],
            "alertname": name, "state": state, "fingerprint": fingerprint, "starts_at": starts_at}


def _record(binding: dict[str, Any], alert: dict[str, Any], sample_ids: list[str]) -> str:
    config = get_config()
    graph = FalkorDB(host=config["FALKORDB_HOST"], port=int(config["FALKORDB_PORT"]),
                     username=config["FALKORDB_ADMIN_USERNAME"],
                     password=config["FALKORDB_ADMIN_PASSWORD"]).select_graph(graph_names().topology)
    result = graph.query(
        "MATCH (d:Deployment {deployment_id:$deployment_id, active:true}) "
        "MERGE (i:Incident {incident_id:$incident_id, tenant_id:'acme'}) "
        "ON CREATE SET i.status='observed', i.execution_mode='real', i.service_id=$service_id, "
        "i.deployment_id=$deployment_id, i.starts_at=$starts_at, i.origin='probe' "
        "SET i.alertname=$alertname, i.fingerprint=$fingerprint, i.recorded_at=$now, "
        "i.observation_ids=$ids, i.alert_status=$alert_status, "
        "i.status=CASE WHEN $alert_status='resolved' AND i.status <> 'recovered' "
        "THEN 'recovered_externally' ELSE i.status END "
        "MERGE (i)-[:OBSERVES_DEPLOYMENT]->(d) RETURN i.status LIMIT 1",
        {"deployment_id": binding["deployment_id"], "incident_id": alert["incident_id"],
         "service_id": binding["service_id"], "starts_at": alert["starts_at"],
         "alertname": alert["alertname"], "fingerprint": alert["fingerprint"],
         "now": datetime.now(timezone.utc).isoformat(), "ids": sample_ids,
         "alert_status": alert["state"]}, timeout=3000).result_set
    if not result:
        raise AlertEvidenceUnavailable("Deployment changed before alert persistence")
    return result[0][0]


async def ingest_alerts(alerts: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        binding = await asyncio.to_thread(bound_deployment, get_config())
    except ValueError as error:
        raise AlertEvidenceUnavailable("No active verified deployment") from error
    normalized = [_normalize(alert, binding) for alert in alerts]
    monitor = MonitoringClient(deployment_id=binding["deployment_id"])
    health = await asyncio.to_thread(monitor.check_health, binding["endpoint"])
    # Validate the whole batch before writing any incident.
    for alert in normalized:
        expected = "healthy" if alert["state"] == "resolved" else (
            "monitoring_unknown" if alert["alertname"] == "ChronosOpsMonitoringUnknown" else "unhealthy")
        if health.status != expected:
            raise AlertEvidenceUnavailable("Current probe evidence does not confirm this notification")
    if health.status == "monitoring_unknown":
        return {"status": "monitoring_unknown", "incidents": [], "reason": health.reason}
    workload_alerts = [alert for alert in normalized if alert["alertname"] == "ChronosOpsDemoHttpDown"]
    if not workload_alerts:
        return {"status": "monitoring_restored", "incidents": [], "reason": health.reason}
    sample_ids = await asyncio.to_thread(persist_health_samples, binding, health.samples)
    recorded = []
    for alert in workload_alerts:
        state = await asyncio.to_thread(_record, binding, alert, sample_ids)
        await asyncio.to_thread(persist_health_samples, binding, health.samples, incident_id=alert["incident_id"])
        recorded.append({"incident_id": alert["incident_id"], "service": binding["service_id"],
                         "status": state, "observation_ids": sample_ids})
    return {"status": "recorded", "incidents": recorded}
