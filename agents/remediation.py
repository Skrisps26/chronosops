"""Remediation node: formulates recovery options based on graph algorithms and evidence."""
from __future__ import annotations

from typing import Any

from agents.tools import failover_routes
from agents.workflow import IncidentState
from scripts.demo_stack import STACK_ARTIFACT


async def remediation_node(state: IncidentState) -> dict[str, Any]:
    turn = state.turn_count + 1

    if state.execution_mode == "simulation":
        # Graph algorithms route computation via SPpaths
        fo_res = await failover_routes(
            source="checkout",
            target="payments-backup",
            max_cost=state.max_cost,
            tenant=state.tenant_id,
        )

        if fo_res.status == "OK":
            rec = fo_res.data
            proposed = {
                "action_kind": "simulate_failover",
                "route": rec.get("recommended_route"),
                "latency_ms": rec.get("latency_ms"),
                "cost": rec.get("cost"),
                "rationale": f"Selected safe alternative route {rec.get('recommended_route')} within cost cap {state.max_cost}",
            }
            return {
                "candidates": fo_res.evidence.get("safe_routes", []),
                "rejected_routes": fo_res.evidence.get("rejected_routes", []),
                "proposed_action": proposed,
                "turn_count": turn,
                "status": "investigating",
            }
        else:
            return {
                "candidates": [],
                "rejected_routes": fo_res.evidence.get("rejected_routes", []),
                "proposed_action": {},
                "turn_count": turn,
                "status": "failed",
                "error_message": "No eligible route found within safety cost cap",
            }

    else:
        # Real execution mode: evaluate live constraint evidence
        # Check if Slack notice indicated backup was unavailable
        slack_notice_present = False
        provenance = state.evidence.get("provenance", {})
        for src in provenance.get("sources", []):
            if src.get("document_id") == "slack-004-backup-maintenance":
                slack_notice_present = True
                break

        # Also resolve actual StartDocumentName from stack outputs if deployed
        doc_name = "chronosops-demo-StartNginx"
        inst_id = None
        if STACK_ARTIFACT.is_file():
            try:
                import json
                stack_info = json.loads(STACK_ARTIFACT.read_text(encoding="utf-8"))
                doc_name = stack_info.get("outputs", {}).get("StartDocumentName", doc_name)
                inst_id = stack_info.get("outputs", {}).get("InstanceId")
            except Exception:
                pass

        proposed = {
            "action_kind": "restart_service",
            "service_id": state.service_id,
            "instance_id": inst_id,
            "document_name": doc_name,
            "rationale": (
                "Verified service failure via HTTP probes. Secondary standby unavailable "
                "for live traffic shifting per Slack operational policy (slack-004). "
                "Restarting primary instance service via reviewed SSM document."
            ),
            "slack_policy_cited": slack_notice_present,
        }

        return {
            "proposed_action": proposed,
            "turn_count": turn,
            "status": "investigating",
        }
