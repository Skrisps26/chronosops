"""Human approvals bind deterministic validated facts, never model assertions."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any

from falkordb import FalkorDB
from redis.exceptions import ResponseError

from agents.safety import ProposalConflict, ProposalUnavailable, validate_proposal
from agents.settings import graph_names, require_tenant
from agents.workflow import IncidentState
from seed.load import get_config


def _json(value: Any) -> str:
    def mapping(item: Any) -> dict[str, Any]:
        if isinstance(item, Mapping):
            return dict(item)
        raise TypeError("Unsupported immutable proposal value")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=mapping, allow_nan=False)


def _graph(tenant: str) -> Any:
    tenant = require_tenant(tenant)
    config = get_config()
    username = config.get(f"FALKORDB_WRITER_{tenant.upper()}_USERNAME")
    password = config.get(f"FALKORDB_WRITER_{tenant.upper()}_PASSWORD")
    if not username or not password:
        raise ProposalUnavailable("writer_credentials_missing", "Tenant proposal writer is not configured")
    graph = FalkorDB(host=config["FALKORDB_HOST"], port=int(config["FALKORDB_PORT"]),
                    username=username, password=password).select_graph(graph_names().topology)
    identities = [item for item in graph.list_constraints()
                  if item["type"] == "UNIQUE" and item["label"] == "ApprovalRecord"
                  and item["properties"] == ["approval_id"] and item["status"] == "OPERATIONAL"]
    if len(identities) != 1:
        raise ProposalUnavailable("approval_schema_unavailable", "Trusted proposal identity schema is required")
    return graph


def _persist(state: IncidentState, validated: Any) -> dict[str, Any]:
    graph = _graph(state.tenant_id)
    now = datetime.now(timezone.utc)
    approval_id = "appr-" + hashlib.sha256(
        f"{state.tenant_id}:{state.incident_id}:{validated.evidence_hash}".encode()).hexdigest()[:32]
    properties = {
        "incident_id": state.incident_id, "tenant_id": state.tenant_id,
        "service_id": state.service_id, "execution_mode": state.execution_mode,
        "incident_type": validated.incident_type, "topology_version": validated.topology_version,
        "action_kind": validated.proposed_action["action_kind"], "evidence_hash": validated.evidence_hash,
        "proposed_action": _json(validated.proposed_action), "binding_json": _json(validated.binding),
        "deployment_binding_json": _json(validated.deployment_binding),
        "runbook_binding_json": _json(validated.runbook_binding), "fault_window_json": _json(validated.fault_window),
        "evidence_versions_json": _json(validated.evidence_versions),
        "rejected_routes_json": _json(state.rejected_routes),
        "observation_ids": list(validated.observation_ids), "max_cost": state.max_cost,
        "validated_at": validated.validated_at, "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(), "status": "pending",
    }
    try:
        rows = graph.query(
            "MERGE (a:ApprovalRecord {approval_id:$id}) ON CREATE SET a += $properties "
            "RETURN properties(a) LIMIT 2", {"id": approval_id, "properties": properties}, timeout=3000).result_set
    except ResponseError:
        rows = graph.ro_query("MATCH (a:ApprovalRecord {approval_id:$id}) RETURN properties(a) LIMIT 2",
                              {"id": approval_id}, timeout=3000).result_set
        if len(rows) != 1:
            raise
    if len(rows) != 1:
        raise ProposalUnavailable("approval_identity_ambiguous", "Approval identity is not unique")
    record = dict(rows[0][0])
    if record.get("evidence_hash") != validated.evidence_hash or record.get("binding_json") != properties["binding_json"]:
        raise ProposalConflict("approval_binding_conflict", "Existing approval binds different facts")
    if record["status"] == "pending" and datetime.fromisoformat(record["expires_at"]) <= now:
        raise ProposalConflict("approval_expired", "Expired evidence requires a new investigation")
    graph.query(
        "MERGE (i:Incident {incident_id:$incident,tenant_id:$tenant}) "
        "ON CREATE SET i.service_id=$service,i.execution_mode=$mode,i.incident_type=$type,"
        "i.origin=$origin,i.starts_at=$now "
        "SET i.approval_id=$approval "
        "SET i.rejected_routes_json=$rejected_json "
        "WITH i MATCH (a:ApprovalRecord {approval_id:$approval}) "
        "MERGE (i)-[:HAS_APPROVAL]->(a)",
        {"incident": state.incident_id, "tenant": state.tenant_id, "service": state.service_id,
         "mode": state.execution_mode, "type": validated.incident_type,
         "origin": "synthetic" if state.execution_mode == "simulation" else "probe",
         "now": now.isoformat(), "approval": approval_id, "rejected_json": _json(state.rejected_routes)}, timeout=3000)
    return record


async def governance_node(state: IncidentState) -> dict[str, Any]:
    import asyncio

    turn = state.turn_count + 1
    if state.status == "failed":
        return {"turn_count": turn, "approval_id": None}
    try:
        validated = await validate_proposal(state)
        record = await asyncio.to_thread(_persist, state, validated)
    except (ProposalConflict, ProposalUnavailable) as error:
        return {"status": "failed", "turn_count": turn, "approval_id": None,
                "error_message": error.reason}
    except Exception:
        return {"status": "failed", "turn_count": turn, "approval_id": None,
                "error_message": "approval_evidence_unavailable"}
    status = "awaiting_approval" if record["status"] == "pending" else record["status"]
    return {"status": status, "approval_id": record["approval_id"], "turn_count": turn,
            "topology_version": validated.topology_version, "incident_type": validated.incident_type,
            "deployment_binding": dict(validated.deployment_binding), "runbook_binding": dict(validated.runbook_binding),
            "fault_window": dict(validated.fault_window), "observation_ids": list(validated.observation_ids),
            "evidence_versions": {key: dict(value) for key, value in validated.evidence_versions.items()},
            "evidence_validated_at": validated.validated_at,
            "proposed_action": json.loads(_json(validated.proposed_action))}
