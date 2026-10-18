"""Triage node: inspects graph blast radius, service ownership, and decision trail."""
from __future__ import annotations

import asyncio
from typing import Any

from agents.knowledge import decision_provenance, who_owns
from agents.mcp_client import FalkorDBMcpClient
from agents import queries
from agents.settings import graph_names
from integrations.monitoring import MonitoringClient
from integrations.health_store import persist_health_samples
from seed.sources import get_published_source
from scripts.fault import _load_window
from agents.tools import blast_radius
from agents.workflow import IncidentState


async def triage_node(state: IncidentState) -> dict[str, Any]:
    """Capture bounded, versioned graph evidence; an unavailable tool is abstention."""
    evidence = dict(state.evidence)
    turn = state.turn_count + 1

    def refused(reason: str) -> dict[str, Any]:
        return {"evidence": evidence, "turn_count": turn, "status": "failed", "error_message": reason}

    try:
        client = FalkorDBMcpClient(tenant=state.tenant_id)
        rows, _ = await client.query_readonly(
            graph_names().topology, queries.CHECK_READY_QUERY, timeout_seconds=3.0, retries=0)
        if len(rows) != 1 or rows[0].get("ready") is not True or not rows[0].get("hash"):
            return refused("topology_unavailable")
        topology_version = str(rows[0]["hash"])
        blast = await blast_radius(state.service_id, tenant=state.tenant_id)
        evidence["blast_radius_tool"] = blast.model_dump(mode="json")
        if blast.status != "OK":
            return refused("blast_radius_" + blast.status.lower())
        evidence["blast_radius"] = blast.data
        evidence["witness_paths"] = blast.evidence.get("witness_paths", [])
        try:
            from agents.memory import retrieve_tenant_memory
            evidence["memory"] = await retrieve_tenant_memory(state.service_id, tenant_id=state.tenant_id)
        except Exception:
            evidence["memory"] = {}
        result: dict[str, Any] = {
            "evidence": evidence, "topology_version": topology_version, "turn_count": turn,
            "incident_type": "simulated_failover", "status": "investigating",
        }
        if state.execution_mode != "real":
            return result
        if state.tenant_id != "acme":
            return refused("real_workload_not_owned")

        versions: dict[str, dict[str, Any]] = {}
        for key, tool in (("ownership", who_owns), ("provenance", decision_provenance)):
            observed = await tool(state.service_id, tenant=state.tenant_id)
            evidence[key + "_tool"] = observed.model_dump(mode="json")
            if observed.status != "OK":
                return refused(key + "_" + observed.status.lower())
            evidence[key] = observed.data
            for citation in observed.evidence.get("citations", []):
                document_id = citation["document_id"]
                if document_id in versions:
                    continue
                source = await asyncio.to_thread(get_published_source, document_id, state.tenant_id)
                if source["content_hash"] != citation["content_hash"] or source["revision"] != citation["revision"]:
                    return refused("source_changed_during_investigation")
                versions[document_id] = {name: source.get(name) for name in
                    ("content_hash", "source_version", "revision", "origin", "service_id")}

        context, _ = await client.query_readonly(
            graph_names().topology,
            "MATCH (i:Incident {incident_id:$incident,tenant_id:$tenant}) "
            "OPTIONAL MATCH (d:Deployment {deployment_id:i.deployment_id,active:true}) "
            "RETURN properties(i) AS incident,properties(d) AS deployment LIMIT 2",
            {"incident": state.incident_id, "tenant": state.tenant_id},
        )
        if len(context) != 1 or not isinstance(context[0].get("deployment"), dict):
            return refused("verified_incident_binding_missing")
        incident = context[0]["incident"]
        binding = context[0]["deployment"]
        if incident.get("origin") != "probe" or incident.get("execution_mode") != "real" \
                or incident.get("service_id") != state.service_id or binding.get("service_id") != state.service_id:
            return refused("incident_binding_mismatch")
        monitor = MonitoringClient(deployment_id=binding["deployment_id"])
        health = await asyncio.to_thread(monitor.check_health, binding["endpoint"])
        evidence["health"] = health.model_dump(mode="json")
        if health.status != "unhealthy" or not health.samples:
            return refused("current_failed_http_evidence_missing")
        observation_ids = await asyncio.to_thread(
            persist_health_samples, binding, health.samples, incident_id=state.incident_id)
        window = await asyncio.to_thread(_load_window)
        result.update({
            "incident_type": "application_outage", "deployment_binding": binding,
            "runbook_binding": {name: binding.get(name) for name in
                ("document_name", "document_version", "document_hash", "recovery_role_arn")},
            "fault_window": window or {}, "observation_ids": observation_ids,
            "evidence_versions": versions,
        })
        return result
    except Exception:
        return refused("investigation_evidence_unavailable")
