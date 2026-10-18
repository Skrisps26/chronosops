"""Create recommendations only from current graph candidates and captured evidence."""
from __future__ import annotations

from typing import Any

from agents.tools import failover_routes
from agents.workflow import IncidentState


async def remediation_node(state: IncidentState) -> dict[str, Any]:
    turn = state.turn_count + 1

    def refuse(reason: str, rejected: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {"status": "failed", "proposed_action": {}, "turn_count": turn,
                "error_message": reason, "rejected_routes": rejected or []}

    if state.status == "failed":
        return {"turn_count": turn, "proposed_action": {}}
    if state.execution_mode == "simulation":
        result = await failover_routes("checkout", "payments-backup", state.max_cost, tenant=state.tenant_id)
        if result.status != "OK":
            return refuse("route_" + result.status.lower(), result.evidence.get("rejected_routes", []))
        if not state.topology_version or result.data.get("topology_version") != state.topology_version:
            return refuse("topology_changed_during_investigation")
        return {
            "status": "investigating", "turn_count": turn,
            "candidates": result.evidence.get("safe_routes", []),
            "rejected_routes": result.evidence.get("rejected_routes", []),
            "evidence": {**state.evidence, "routes": result.model_dump(mode="json")},
            "proposed_action": {"action_kind": "simulate_failover", "source": "checkout",
                                "target": "payments-backup", "route": result.data["recommended_route"],
                                "latency_ms": result.data["latency_ms"], "cost": result.data["cost"],
                                "rationale": "Graph-returned healthy route within the declared search bounds and cost cap."},
        }

    binding = state.deployment_binding
    required = ("deployment_id", "service_id", "instance_id", "document_name", "document_version", "document_hash")
    if not all(binding.get(field) for field in required) or binding["service_id"] != state.service_id:
        return refuse("verified_restart_binding_missing")
    policy_sources = sorted(document_id for document_id, version in state.evidence_versions.items()
                            if version.get("origin") == "slack" and version.get("service_id") == state.service_id)
    if not policy_sources or not state.observation_ids:
        return refuse("live_policy_or_failed_probe_evidence_missing")
    return {
        "status": "investigating", "turn_count": turn,
        "proposed_action": {
            "action_kind": "restart_service", "service_id": state.service_id,
            "deployment_id": binding["deployment_id"], "instance_id": binding["instance_id"],
            "document_name": binding["document_name"], "document_version": binding["document_version"],
            "document_hash": binding["document_hash"], "policy_source_ids": policy_sources,
            "rationale": "Captured failed HTTP observations " + ", ".join(state.observation_ids)
                         + "; stored restart binding " + binding["document_name"] + "@" + str(binding["document_version"])
                         + "; current cited Slack sources " + ", ".join(policy_sources) + ". Safety validation is required.",
        },
    }
