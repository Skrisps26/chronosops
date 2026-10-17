"""Governance node: enforces policy boundaries and creates immutable server-side approval records."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from falkordb import FalkorDB

from agents.workflow import IncidentState
from seed.load import get_config
from agents.settings import graph_names


def compute_evidence_hash(evidence: dict[str, Any], proposed: dict[str, Any]) -> str:
    serialized = json.dumps({"evidence": evidence, "proposed": proposed}, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


async def governance_node(state: IncidentState) -> dict[str, Any]:
    turn = state.turn_count + 1

    if state.status == "failed":
        return {"turn_count": turn}

    if not state.proposed_action:
        return {
            "status": "failed",
            "error_message": "Governance rejected: no actionable proposal generated",
            "turn_count": turn,
        }

    # Policy checks for real mode
    if state.execution_mode == "real":
        if state.tenant_id != "acme":
            return {
                "status": "failed",
                "error_message": f"Tenant '{state.tenant_id}' is not authorized to operate real AWS workload",
                "turn_count": turn,
            }

        # Verify proposal has required restart action
        action_kind = state.proposed_action.get("action_kind")
        if action_kind != "restart_service":
            return {
                "status": "failed",
                "error_message": f"Disallowed action kind '{action_kind}' for real execution",
                "turn_count": turn,
            }

    # Compute hash of evidence and proposal
    ev_hash = compute_evidence_hash(state.evidence, state.proposed_action)

    # Server-generated approval ID and 5-minute expiry
    approval_id = f"appr-{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(minutes=5)).isoformat()

    # Persist approval record in ops_topology
    config = get_config()
    db = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=config["FALKORDB_ADMIN_USERNAME"],
        password=config.get("FALKORDB_ADMIN_PASSWORD", ""),
    )
    graph = db.select_graph(graph_names().topology)

    graph.query(
        "MERGE (a:ApprovalRecord {approval_id: $approval_id}) "
        "SET a.incident_id = $incident_id, "
        "    a.tenant_id = $tenant, "
        "    a.execution_mode = $mode, "
        "    a.service_id = $service_id, "
        "    a.action_kind = $action_kind, "
        "    a.evidence_hash = $ev_hash, "
        "    a.proposed_action = $proposed_json, "
        "    a.created_at = $now_ts, "
        "    a.expires_at = $expires_at, "
        "    a.status = 'pending'",
        {
            "approval_id": approval_id,
            "incident_id": state.incident_id,
            "tenant": state.tenant_id,
            "mode": state.execution_mode,
            "service_id": state.service_id,
            "action_kind": state.proposed_action.get("action_kind", ""),
            "ev_hash": ev_hash,
            "proposed_json": json.dumps(state.proposed_action),
            "now_ts": now.isoformat(),
            "expires_at": expires_at,
        },
    )

    return {
        "approval_id": approval_id,
        "status": "awaiting_approval",
        "turn_count": turn,
    }
