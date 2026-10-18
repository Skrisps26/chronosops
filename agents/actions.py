"""Atomic human-approval lifecycle for simulated and bounded real recovery."""
from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from falkordb import FalkorDB

from agents.safety import ProposalConflict, ProposalUnavailable, validate_proposal
from agents.settings import graph_names, require_tenant
from agents.workflow import IncidentState
from integrations.aws_actions import AwsRecoveryClient, RecoveryExecutionError
from integrations.health_store import persist_health_samples
from integrations.monitoring import MonitoringClient
from seed.load import get_config
from seed.sources import SourceUnavailable, evidence_state_guard


class ActionConflictError(Exception):
    """The approval is stale, expired, or has already transitioned."""


class ActionNotFoundError(Exception):
    """The tenant does not own the requested approval and incident."""


class ActionUnavailableError(Exception):
    """Current authority or durable state is unavailable."""


_FINAL_STATUSES = frozenset({"applied", "recovered", "failed", "recovery_failed", "rejected", "expired"})
_UNRESOLVED_REAL_STATUSES = frozenset({"executing", "verifying", "execution_unknown", "verification_unknown"})


def _json(value: Any) -> str:
    def mapping(item: Any) -> dict[str, Any]:
        if isinstance(item, Mapping):
            return dict(item)
        raise TypeError("Approval contains unsupported immutable evidence")

    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=mapping)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _graph(tenant_id: str) -> Any:
    try:
        tenant_id = require_tenant(tenant_id)
    except ValueError as error:
        raise ActionNotFoundError("Approval not found") from error
    config = get_config()
    username = config.get(f"FALKORDB_WRITER_{tenant_id.upper()}_USERNAME")
    password = config.get(f"FALKORDB_WRITER_{tenant_id.upper()}_PASSWORD")
    if not username or not password:
        raise ActionUnavailableError("Action writer is unavailable")
    return FalkorDB(
        host=config["FALKORDB_HOST"], port=int(config["FALKORDB_PORT"]),
        username=username, password=password,
    ).select_graph(graph_names().topology)


def _read_record(graph: Any, incident_id: str, approval_id: str, tenant_id: str) -> dict[str, Any]:
    rows = graph.ro_query(
        "MATCH (a:ApprovalRecord {approval_id:$approval_id, tenant_id:$tenant_id, incident_id:$incident_id}) "
        "RETURN properties(a) LIMIT 2",
        {"approval_id": approval_id, "tenant_id": tenant_id, "incident_id": incident_id}, timeout=3000,
    ).result_set
    if not rows:
        raise ActionNotFoundError("Approval not found")
    if len(rows) != 1:
        raise ActionUnavailableError("Approval identity is ambiguous")
    return dict(rows[0][0])


def _mapping_json(record: Mapping[str, Any], field: str, *, required: bool = True) -> dict[str, Any]:
    raw = record.get(field)
    if raw is None and not required:
        return {}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError) as error:
        raise ActionConflictError("Approval contains invalid immutable evidence") from error
    if not isinstance(value, Mapping) or (required and not value):
        raise ActionConflictError("Approval contains incomplete immutable evidence")
    return dict(value)


def _state_from_record(record: Mapping[str, Any]) -> IncidentState:
    binding = _mapping_json(record, "binding_json")
    proposed = _mapping_json(record, "proposed_action")
    deployment = _mapping_json(record, "deployment_binding_json", required=False)
    runbook = _mapping_json(record, "runbook_binding_json", required=False)
    fault_window = _mapping_json(record, "fault_window_json", required=False)
    evidence_versions = _mapping_json(record, "evidence_versions_json", required=False)
    mode = record.get("execution_mode")
    if mode not in {"real", "simulation"}:
        raise ActionConflictError("Approval has an unsupported execution mode")
    observations = record.get("observation_ids") or []
    if not isinstance(observations, list):
        raise ActionConflictError("Approval contains invalid observation evidence")
    service_id = record.get("service_id")
    incident_id = record.get("incident_id")
    tenant_id = record.get("tenant_id")
    incident_type = record.get("incident_type")
    topology_version = record.get("topology_version")
    if not all(isinstance(item, str) and item for item in (service_id, incident_id, tenant_id, incident_type, topology_version)):
        raise ActionConflictError("Approval contains incomplete immutable evidence")
    return IncidentState(
        incident_id=incident_id, tenant_id=tenant_id, service_id=service_id,
        execution_mode=mode, incident_type=incident_type, topology_version=topology_version,
        max_cost=record.get("max_cost", 12), proposed_action=proposed,
        deployment_binding=deployment, runbook_binding=runbook, fault_window=fault_window,
        observation_ids=observations, evidence_versions=evidence_versions,
        evidence_validated_at=record.get("validated_at"),
        # This is the captured authoritative projection stored by governance;
        # it deliberately cannot be replaced by caller/model input.
        evidence={"approval_binding": binding}, approval_id=record.get("approval_id"),
    )


async def _revalidate(record: Mapping[str, Any]) -> Any:
    try:
        validated = await validate_proposal(_state_from_record(record))
    except ProposalConflict as error:
        raise ActionConflictError("Approval evidence is no longer current") from error
    except ProposalUnavailable as error:
        raise ActionUnavailableError("Current approval evidence is unavailable") from error
    except ActionConflictError:
        raise
    except Exception as error:
        raise ActionUnavailableError("Current approval evidence is unavailable") from error
    if (
        validated.evidence_hash != record.get("evidence_hash")
        or _json(validated.binding) != _json(_mapping_json(record, "binding_json"))
    ):
        raise ActionConflictError("Approval evidence changed; a new approval is required")
    if _json(validated.proposed_action) != _json(_mapping_json(record, "proposed_action")):
        raise ActionConflictError("Approved action changed; a new approval is required")
    return validated


def _source_is_current(tenant_id: str, evidence_versions: Mapping[str, Any]) -> None:
    """Perform the final native source check while source publication is fenced."""
    if not evidence_versions:
        return
    config = get_config()
    username = config.get(f"FALKORDB_READER_{tenant_id.upper()}_USERNAME")
    password = config.get(f"FALKORDB_READER_{tenant_id.upper()}_PASSWORD")
    if not username or not password:
        raise ActionUnavailableError("Source evidence reader is unavailable")
    expected: dict[str, Mapping[str, Any]] = {
        document_id: version for document_id, version in evidence_versions.items()
        if isinstance(document_id, str) and isinstance(version, Mapping)
    }
    if len(expected) != len(evidence_versions):
        raise ActionConflictError("Approval source evidence is malformed")
    company = FalkorDB(
        host=config["FALKORDB_HOST"],
        port=int(config["FALKORDB_PORT"]),
        username=username,
        password=password,
    ).select_graph(graph_names().company)
    rows = company.ro_query(
        "MATCH (m:Meta {id:'company_brain_meta'}) "
        "OPTIONAL MATCH (s:SourceRecord) WHERE s.document_id IN $ids "
        "RETURN m.ready AS ready, m.schema_ready AS schema_ready, collect(properties(s)) AS sources",
        {"ids": list(expected)},
        timeout=3000,
    ).result_set
    if len(rows) != 1 or rows[0][0] is not True or rows[0][1] is not True:
        raise ActionUnavailableError("Current source evidence is unavailable")
    sources = {item.get("document_id"): item for item in rows[0][2] if isinstance(item, Mapping)}
    if set(sources) != set(expected):
        raise ActionConflictError("Approval source evidence changed")
    for document_id, version in expected.items():
        source = sources[document_id]
        if source.get("status") != "published" or any(
            source.get(field) != version.get(field)
            for field in ("content_hash", "source_version", "revision", "service_id")
        ):
            raise ActionConflictError("Approval source evidence changed")


_DEPLOYMENT_CLAIM_FIELDS = (
    "deployment_id",
    "tenant_id",
    "service_id",
    "account_id",
    "region",
    "instance_id",
    "stack_id",
    "document_name",
    "document_version",
    "document_hash",
    "recovery_role_arn",
    "operator",
    "expires_at",
    "endpoint",
)


def _simulation_claim_inputs(
    record: Mapping[str, Any],
    validated: Any,
) -> tuple[list[str], list[dict[str, str]], int, int, str]:
    """Return the immutable route facts that the native write must recheck."""
    action = _mapping_json(record, "proposed_action")
    route = action.get("route")
    route_cost = action.get("cost")
    max_cost = record.get("max_cost")
    binding = _mapping_json(record, "binding_json")
    if (
        action.get("action_kind") != "simulate_failover"
        or not isinstance(route, list)
        or len(route) < 2
        or any(not isinstance(node, str) or not node for node in route)
        or not isinstance(route_cost, int)
        or isinstance(route_cost, bool)
        or not isinstance(max_cost, int)
        or isinstance(max_cost, bool)
        or max_cost <= 0
        or route_cost > max_cost
        or binding.get("route") != route
        or binding.get("max_cost") != max_cost
    ):
        raise ActionConflictError("Approval route evidence is incomplete")
    action_json = _json(action)
    if action_json != _json(validated.proposed_action):
        raise ActionConflictError("Approved action changed; a new approval is required")
    return route, [
        {"source": route[index], "target": route[index + 1]}
        for index in range(len(route) - 1)
    ], route_cost, max_cost, action_json


def _real_claim_inputs(record: Mapping[str, Any], validated: Any) -> tuple[dict[str, Any], str, list[str]]:
    """Return the complete verified deployment binding for the native claim."""
    binding = _mapping_json(record, "deployment_binding_json")
    if any(not isinstance(binding.get(field), str) or not binding[field] for field in _DEPLOYMENT_CLAIM_FIELDS):
        raise ActionConflictError("Approval contains incomplete recovery evidence")
    action = _mapping_json(record, "proposed_action")
    action_json = _json(action)
    if action_json != _json(validated.proposed_action):
        raise ActionConflictError("Approved action changed; a new approval is required")
    observations = record.get("observation_ids") or []
    if (
        not isinstance(observations, list)
        or not observations
        or any(not isinstance(observation, str) or not observation for observation in observations)
        or len(set(observations)) != len(observations)
    ):
        raise ActionConflictError("Approval contains incomplete recovery evidence")
    return binding, action_json, observations

def _claim(
    graph: Any,
    record: Mapping[str, Any],
    validated: Any,
    now: str,
    claim_token: str,
    comment: str,
) -> dict[str, Any] | None:
    common = {
        "approval_id": record["approval_id"],
        "incident_id": record["incident_id"],
        "tenant_id": record["tenant_id"],
        "now": now,
        "evidence_hash": record["evidence_hash"],
        "binding_json": _json(validated.binding),
        "topology_version": record["topology_version"],
        "meta_hash": validated.topology_version,
        "claim_token": claim_token,
        "comment": comment,
    }
    if record["execution_mode"] == "simulation":
        route, route_pairs, route_cost, max_cost, action_json = _simulation_claim_inputs(record, validated)
        rows = graph.query(
            "MATCH (a:ApprovalRecord {approval_id:$approval_id,tenant_id:$tenant_id,incident_id:$incident_id}) "
            "MATCH (i:Incident {tenant_id:$tenant_id,incident_id:$incident_id}) "
            "MATCH (:Meta {id:'ops_topology_meta',ready:true,hash:$meta_hash}) "
            "UNWIND $route_pairs AS route_pair "
            "MATCH (:Service {service_id:route_pair.source})-[edge:ROUTES_TO]->(:Service {service_id:route_pair.target}) "
            "WITH a,i,count(edge) AS edge_count,sum(edge.cost) AS current_cost,"
            "sum(CASE WHEN edge.health='healthy' THEN 1 ELSE 0 END) AS healthy_edge_count "
            "UNWIND $route AS route_node_id "
            "MATCH (route_node:Service {service_id:route_node_id}) "
            "WITH a,i,edge_count,current_cost,healthy_edge_count,count(route_node) AS node_count,"
            "sum(CASE WHEN route_node.health='healthy' THEN 1 ELSE 0 END) AS healthy_node_count "
            "WHERE a.status='pending' AND a.expires_at > $now AND a.evidence_hash=$evidence_hash "
            "AND a.binding_json=$binding_json AND a.topology_version=$topology_version "
            "AND a.proposed_action=$action_json AND a.max_cost=$max_cost "
            "AND i.approval_id=$approval_id "
            "AND edge_count=size($route_pairs) AND healthy_edge_count=size($route_pairs) "
            "AND current_cost=$route_cost AND current_cost <= $max_cost "
            "AND node_count=size($route) AND healthy_node_count=size($route) "
            "SET a.status='applied',a.applied_at=$now,a.claim_token=$claim_token,a.memory_pending=true,"
            "i.status='applied',i.applied_at=$now,i.memory_pending=true "
            "WITH a MERGE (traffic:TrafficState {tenant_id:$tenant_id,incident_id:$incident_id}) "
            "SET traffic.active_route=$route,traffic.applied_at=$now,traffic.action='simulate_failover' "
            "RETURN properties(a) LIMIT 2",
            {
                **common,
                "route": route,
                "route_pairs": route_pairs,
                "route_cost": route_cost,
                "max_cost": max_cost,
                "action_json": action_json,
            },
            timeout=3000,
        ).result_set
    else:
        binding, action_json, observations = _real_claim_inputs(record, validated)
        deployment_params = {
            f"deployment_{field}": binding[field]
            for field in _DEPLOYMENT_CLAIM_FIELDS
        }
        rows = graph.query(
            "MATCH (a:ApprovalRecord {approval_id:$approval_id,tenant_id:$tenant_id,incident_id:$incident_id}) "
            "MATCH (i:Incident {tenant_id:$tenant_id,incident_id:$incident_id}) "
            "MATCH (slot:RecoverySlot {id:'real'}) "
            "MATCH (:Meta {id:'ops_topology_meta',ready:true,hash:$meta_hash}) "
            "MATCH (deployment:Deployment {deployment_id:$deployment_deployment_id,active:true,"
            "tenant_id:$deployment_tenant_id}) "
            "OPTIONAL MATCH (i)-[:HEALTH_EVIDENCE]->(h:HealthObservation) WHERE h.id IN $observations "
            "WITH a,i,slot,deployment,count(DISTINCT h) AS health_count "
            "WHERE a.status='pending' AND a.expires_at > $now AND a.evidence_hash=$evidence_hash "
            "AND a.binding_json=$binding_json AND a.topology_version=$topology_version "
            "AND a.proposed_action=$action_json AND i.approval_id=$approval_id "
            "AND i.deployment_id=$deployment_deployment_id AND health_count=size($observations) "
            "AND deployment.service_id=$deployment_service_id AND deployment.account_id=$deployment_account_id "
            "AND deployment.region=$deployment_region AND deployment.instance_id=$deployment_instance_id "
            "AND deployment.stack_id=$deployment_stack_id AND deployment.document_name=$deployment_document_name "
            "AND deployment.document_version=$deployment_document_version "
            "AND deployment.document_hash=$deployment_document_hash "
            "AND deployment.recovery_role_arn=$deployment_recovery_role_arn "
            "AND deployment.operator=$deployment_operator AND deployment.expires_at=$deployment_expires_at "
            "AND deployment.endpoint=$deployment_endpoint "
            "AND slot.status IN ['free','released'] "
            "SET a.status='executing',a.executing_at=$now,a.claim_token=$claim_token,a.command_comment=$comment,"
            "i.status='executing',i.executing_at=$now,"
            "slot.status='executing',slot.approval_id=$approval_id,slot.incident_id=$incident_id,slot.tenant_id=$tenant_id "
            "RETURN properties(a) LIMIT 2",
            {**common, **deployment_params, "observations": observations, "action_json": action_json},
            timeout=3000,
        ).result_set
    if len(rows) > 1:
        raise ActionUnavailableError("Approval identity is ambiguous")
    return dict(rows[0][0]) if rows else None


def _claim_with_source_fence(
    graph: Any,
    record: Mapping[str, Any],
    validated: Any,
    now: str,
    claim_token: str,
    comment: str,
) -> dict[str, Any] | None:
    """Fence final source publication around source refresh and topology claim."""
    with evidence_state_guard(blocking=False):
        _source_is_current(record["tenant_id"], _mapping_json(record, "evidence_versions_json", required=False))
        return _claim(graph, record, validated, now, claim_token, comment)


def _stored_result(record: Mapping[str, Any], *, duplicate: bool = False) -> dict[str, Any]:
    status = record.get("status")
    result = {
        "status": status, "incident_id": record.get("incident_id"), "approval_id": record.get("approval_id"),
        "execution_mode": record.get("execution_mode"),
        "already_completed": duplicate and status in _FINAL_STATUSES,
    }
    for field in ("command_id", "command_completed_at", "recovery_sample_ids", "applied_at", "recovered_at", "error_code", "ssm_status"):
        if record.get(field) is not None:
            result[field] = record[field]
    if record.get("execution_mode") == "simulation":
        route = _mapping_json(record, "proposed_action", required=False).get("route")
        if route:
            result["active_route"] = route
    return result


def _release_slot(graph: Any, record: Mapping[str, Any], status: str, **properties: Any) -> dict[str, Any]:
    rows = graph.query(
        "MATCH (a:ApprovalRecord {approval_id:$approval_id,tenant_id:$tenant_id,incident_id:$incident_id}) "
        "MATCH (i:Incident {tenant_id:$tenant_id,incident_id:$incident_id}) "
        "MATCH (slot:RecoverySlot {id:'real',approval_id:$approval_id}) "
        "WHERE a.status IN ['executing','verifying','execution_unknown','verification_unknown'] "
        "SET a.status=$status,a.outcome_at=$now,a.memory_pending=true,a += $properties, i.status=$status,i.outcome_at=$now,i.memory_pending=true, "
        "slot.status='released',slot.released_at=$now "
        "RETURN properties(a) LIMIT 2",
        {"approval_id": record["approval_id"], "tenant_id": record["tenant_id"], "incident_id": record["incident_id"],
         "status": status, "properties": properties, "now": _now()}, timeout=3000,
    ).result_set
    if len(rows) != 1:
        raise ActionUnavailableError("Recovery outcome could not be persisted")
    return dict(rows[0][0])


def _unknown(graph: Any, record: Mapping[str, Any], status: str, **properties: Any) -> dict[str, Any]:
    rows = graph.query(
        "MATCH (a:ApprovalRecord {approval_id:$approval_id,tenant_id:$tenant_id,incident_id:$incident_id}) "
        "MATCH (i:Incident {tenant_id:$tenant_id,incident_id:$incident_id}) "
        "WHERE a.status IN ['executing','verifying','execution_unknown','verification_unknown'] "
        "SET a.status=$status,a.outcome_at=$now,a += $properties,i.status=$status,i.outcome_at=$now "
        "RETURN properties(a) LIMIT 2",
        {"approval_id": record["approval_id"], "tenant_id": record["tenant_id"], "incident_id": record["incident_id"],
         "status": status, "properties": properties, "now": _now()}, timeout=3000,
    ).result_set
    if len(rows) != 1:
        raise ActionUnavailableError("Uncertain recovery state could not be persisted")
    return dict(rows[0][0])


def _save_command_id(graph: Any, record: Mapping[str, Any], command_id: str) -> dict[str, Any]:
    rows = graph.query(
        "MATCH (a:ApprovalRecord {approval_id:$approval_id,tenant_id:$tenant_id,incident_id:$incident_id,status:'executing'}) "
        "MATCH (:RecoverySlot {id:'real',approval_id:$approval_id,status:'executing'}) "
        "SET a.command_id=$command_id,a.command_id_persisted_at=$now "
        "RETURN properties(a) LIMIT 2",
        {"approval_id": record["approval_id"], "tenant_id": record["tenant_id"], "incident_id": record["incident_id"],
         "command_id": command_id, "now": _now()}, timeout=3000,
    ).result_set
    if len(rows) != 1:
        raise ActionUnavailableError("Command identifier could not be persisted")
    return dict(rows[0][0])


def _start_verifying(graph: Any, record: Mapping[str, Any], completed_at: float, ssm_status: str) -> dict[str, Any]:
    rows = graph.query(
        "MATCH (a:ApprovalRecord {approval_id:$approval_id,tenant_id:$tenant_id,incident_id:$incident_id,status:'executing'}) "
        "MATCH (i:Incident {tenant_id:$tenant_id,incident_id:$incident_id}) "
        "SET a.status='verifying',a.command_completed_at=$completed_at,a.ssm_status=$ssm_status,a.verifying_at=$now, "
        "i.status='verifying',i.command_completed_at=$completed_at,i.verifying_at=$now "
        "RETURN properties(a) LIMIT 2",
        {"approval_id": record["approval_id"], "tenant_id": record["tenant_id"], "incident_id": record["incident_id"],
         "completed_at": completed_at, "ssm_status": ssm_status, "now": _now()}, timeout=3000,
    ).result_set
    if len(rows) != 1:
        raise ActionUnavailableError("Command completion could not be persisted")
    return dict(rows[0][0])


def _valid_samples(binding: Mapping[str, Any], samples: list[Any], completed_at: float) -> bool:
    endpoint = binding.get("endpoint")
    deployment_id = binding.get("deployment_id")
    ids: set[str] = set()
    timestamps: set[float] = set()
    if len(samples) != 3 or not isinstance(endpoint, str) or not isinstance(deployment_id, str):
        return False
    for sample in samples:
        if (getattr(sample, "id", None) in ids or getattr(sample, "observed_at", None) in timestamps
                or getattr(sample, "deployment_id", None) != deployment_id or getattr(sample, "endpoint", None) != endpoint
                or getattr(sample, "exporter_up", None) is not True or getattr(sample, "probe_success", None) is not True
                or not isinstance(getattr(sample, "observed_at", None), (int, float))
                or sample.observed_at <= completed_at):
            return False
        ids.add(sample.id)
        timestamps.add(sample.observed_at)
    return True

async def _poll_and_verify(graph: Any, record: Mapping[str, Any], command_id: str) -> dict[str, Any]:
    binding = _mapping_json(record, "deployment_binding_json")
    completed_at = record.get("command_completed_at")
    if record.get("status") in {"verifying", "verification_unknown"} and isinstance(completed_at, (int, float)):
        verifying = record
    else:
        try:
            outcome = await AwsRecoveryClient(region=binding.get("region")).poll_restart(command_id, binding)
        except Exception:
            return _unknown(graph, record, "execution_unknown", command_id=command_id)
        outcome_status = getattr(outcome, "status", "execution_unknown")
        ssm_status = getattr(outcome, "ssm_status", "unknown")
        error_code = getattr(outcome, "error_code", None)
        if outcome_status == "execution_unknown":
            return _unknown(graph, record, "execution_unknown", command_id=command_id,
                            ssm_status=ssm_status, error_code=error_code)
        if outcome_status != "success" or not isinstance(getattr(outcome, "completed_at", None), (int, float)):
            return _release_slot(graph, record, "failed", command_id=command_id,
                                 ssm_status=ssm_status, error_code=error_code)
        completed_at = float(outcome.completed_at)
        verifying = _start_verifying(graph, record, completed_at, ssm_status)
    try:
        assessment = await MonitoringClient(deployment_id=binding["deployment_id"]).verify_recovery_samples(
            binding["endpoint"], after=float(completed_at), required_samples=3,
        )
    except Exception:
        return _unknown(graph, verifying, "verification_unknown", command_id=command_id)
    samples = list(getattr(assessment, "samples", []))
    if (
        getattr(assessment, "status", None) == "healthy"
        and getattr(assessment, "healthy", False)
        and _valid_samples(binding, samples, float(completed_at))
    ):
        try:
            sample_ids = await asyncio.to_thread(
                persist_health_samples, binding, samples, incident_id=record["incident_id"],
            )
        except Exception:
            return _unknown(graph, verifying, "verification_unknown", command_id=command_id)
        return _release_slot(
            graph, verifying, "recovered", command_id=command_id,
            recovery_sample_ids=sample_ids,
            recovery_observed_at=[sample.observed_at for sample in samples],
            recovered_at=_now(),
        )
    if getattr(assessment, "status", None) == "unhealthy":
        return _release_slot(
            graph, verifying, "recovery_failed", command_id=command_id,
            verification_reason=getattr(assessment, "reason", "unhealthy"),
        )
    return _unknown(
        graph, verifying, "verification_unknown", command_id=command_id,
        verification_reason=getattr(assessment, "reason", "monitoring_unknown"),
    )


async def approve_action(incident_id: str, approval_id: str, tenant_id: str = "acme") -> dict[str, Any]:
    """Atomically claim one current approval and durably execute its exact action."""
    graph = await asyncio.to_thread(_graph, tenant_id)
    record = await asyncio.to_thread(_read_record, graph, incident_id, approval_id, tenant_id)
    if record.get("status") != "pending":
        if record.get("status") in ("applied", "recovered") or record.get("status") in _UNRESOLVED_REAL_STATUSES:
            return _stored_result(record, duplicate=True)
        raise ActionConflictError("Approval cannot be approved in its current state")
    expires_at = record.get("expires_at")
    if not isinstance(expires_at, str) or datetime.fromisoformat(expires_at.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
        await asyncio.to_thread(_expire, graph, record)
        raise ActionConflictError("Approval proposal has expired")
    if record.get("execution_mode") == "real" and get_config().get("REAL_RECOVERY_ENABLED", "false").lower() != "true":
        raise ActionConflictError("Real recovery is disabled")
    try:
        validated = await _revalidate(record)
    except ActionConflictError:
        latest = await asyncio.to_thread(_read_record, graph, incident_id, approval_id, tenant_id)
        if latest.get("status") in ("applied", "recovered") or latest.get("status") in _UNRESOLVED_REAL_STATUSES:
            return _stored_result(latest, duplicate=True)
        raise
    comment = f"ChronosOps incident={incident_id} approval={approval_id}" if len(f"ChronosOps incident={incident_id} approval={approval_id}") <= 100 else f"ChronosOps {incident_id} {approval_id}"
    if len(comment) > 100:
        raise ActionConflictError("Approval identifiers exceed the SSM comment limit")
    claim_token = str(uuid.uuid4())
    try:
        claim_fn = _claim_with_source_fence if record["execution_mode"] == "real" else _claim
        claimed = await asyncio.to_thread(claim_fn, graph, record, validated, _now(), claim_token, comment)
    except SourceUnavailable as error:
        raise ActionUnavailableError("Current source evidence is unavailable") from error
    if claimed is None:
        latest = await asyncio.to_thread(_read_record, graph, incident_id, approval_id, tenant_id)
        if latest.get("status") in ("applied", "recovered") or latest.get("status") in _UNRESOLVED_REAL_STATUSES:
            return _stored_result(latest, duplicate=True)
        raise ActionConflictError("Approval is stale or was transitioned by another operator")
    if record["execution_mode"] == "simulation":
        res = _stored_result(claimed)
        try:
            from agents.memory import consolidate_incident
            await consolidate_incident(incident_id, tenant_id=tenant_id)
        except Exception:
            pass
        return res
    binding = _mapping_json(claimed, "deployment_binding_json")
    try:
        command_id = await AwsRecoveryClient(region=binding.get("region")).submit_restart(binding, comment)
    except RecoveryExecutionError:
        return _stored_result(await asyncio.to_thread(_release_slot, graph, claimed, "failed", error_code="submission_rejected"))
    except Exception:
        return _stored_result(await asyncio.to_thread(_unknown, graph, claimed, "execution_unknown"))
    try:
        executing = await asyncio.to_thread(_save_command_id, graph, claimed, command_id)
    except Exception:
        try:
            await asyncio.to_thread(_unknown, graph, claimed, "execution_unknown")
        except Exception:
            pass
        raise ActionUnavailableError("Command submission identity is not durably recorded")
    final_outcome = await _poll_and_verify(graph, executing, command_id)
    res = _stored_result(final_outcome)
    try:
        from agents.memory import consolidate_incident
        await consolidate_incident(incident_id, tenant_id=tenant_id)
    except Exception:
        pass
    return res


def _expire(graph: Any, record: Mapping[str, Any]) -> None:
    graph.query(
        "MATCH (a:ApprovalRecord {approval_id:$approval_id,tenant_id:$tenant_id,incident_id:$incident_id,status:'pending'}) "
        "WHERE a.expires_at <= $now SET a.status='expired',a.expired_at=$now "
        "WITH a OPTIONAL MATCH (i:Incident {tenant_id:$tenant_id,incident_id:$incident_id}) "
        "SET i.status='expired',i.expired_at=$now",
        {"approval_id": record["approval_id"], "tenant_id": record["tenant_id"],
         "incident_id": record["incident_id"], "now": _now()}, timeout=3000,
    )


async def reject_action(incident_id: str, approval_id: str, tenant_id: str = "acme") -> dict[str, Any]:
    """Atomically reject only an unexpired pending approval."""
    graph = await asyncio.to_thread(_graph, tenant_id)
    now = _now()
    rows = await asyncio.to_thread(
        lambda: graph.query(
            "MATCH (a:ApprovalRecord {approval_id:$approval_id,tenant_id:$tenant_id,incident_id:$incident_id,status:'pending'}) "
            "WHERE a.expires_at > $now SET a.status='rejected',a.rejected_at=$now "
            "WITH a OPTIONAL MATCH (i:Incident {tenant_id:$tenant_id,incident_id:$incident_id}) "
            "SET i.status='rejected',i.rejected_at=$now RETURN properties(a) LIMIT 2",
            {"approval_id": approval_id, "tenant_id": tenant_id, "incident_id": incident_id, "now": now}, timeout=3000,
        ).result_set
    )
    if len(rows) == 1:
        return _stored_result(dict(rows[0][0]))
    record = await asyncio.to_thread(_read_record, graph, incident_id, approval_id, tenant_id)
    if record.get("status") == "pending" and isinstance(record.get("expires_at"), str) and record["expires_at"] <= now:
        await asyncio.to_thread(_expire, graph, record)
    raise ActionConflictError("Approval cannot be rejected in its current state")


async def get_reconcilable_action(incident_id: str, approval_id: str, tenant_id: str = "acme") -> dict[str, Any]:
    """Return tenant-scoped unresolved command state for an explicit operator action."""
    graph = await asyncio.to_thread(_graph, tenant_id)
    record = await asyncio.to_thread(_read_record, graph, incident_id, approval_id, tenant_id)
    if record.get("execution_mode") != "real" or record.get("status") not in _UNRESOLVED_REAL_STATUSES:
        raise ActionConflictError("Action is not reconcilable")
    return _stored_result(record) | {
        "approval": dict(record),
        "deployment_binding": _mapping_json(record, "deployment_binding_json"),
        "command_comment": record.get("command_comment"),
        "executing_at": record.get("executing_at"),
    }


async def reconcile_known_command(incident_id: str, approval_id: str, command_id: str, tenant_id: str = "acme") -> dict[str, Any]:
    """Attach one operator-discovered command to its held singleton slot, then read it."""
    if not isinstance(command_id, str) or not command_id:
        raise ActionConflictError("Command identifier is required")
    graph = await asyncio.to_thread(_graph, tenant_id)
    record = await asyncio.to_thread(_read_record, graph, incident_id, approval_id, tenant_id)
    existing = record.get("command_id")
    if existing and existing != command_id:
        raise ActionConflictError("Action already has a different command identifier")
    if record.get("execution_mode") != "real" or record.get("status") not in _UNRESOLVED_REAL_STATUSES:
        raise ActionConflictError("Action is not reconcilable")
    rows = await asyncio.to_thread(
        lambda: graph.query(
            "MATCH (a:ApprovalRecord {approval_id:$approval_id,tenant_id:$tenant_id,incident_id:$incident_id}) "
            "MATCH (slot:RecoverySlot {id:'real',approval_id:$approval_id,tenant_id:$tenant_id,incident_id:$incident_id,status:'executing'}) "
            "WHERE a.status IN ['executing','verifying','execution_unknown','verification_unknown'] "
            "AND (a.command_id=$command_id OR (a.command_id IS NULL AND a.status='execution_unknown')) "
            "SET a.command_id=$command_id,a.command_id_persisted_at=coalesce(a.command_id_persisted_at,$now) "
            "RETURN properties(a) LIMIT 2",
            {"approval_id": approval_id, "tenant_id": tenant_id, "incident_id": incident_id,
             "command_id": command_id, "now": _now()}, timeout=3000,
        ).result_set
    )
    if len(rows) != 1:
        raise ActionConflictError("Recovery slot is not reserved for this action")
    attached = dict(rows[0][0])
    if attached.get("status") in {"verifying", "verification_unknown"} and isinstance(attached.get("command_completed_at"), (int, float)):
        # A prior poll proved completion; resume only the safe verification phase.
        return _stored_result(await _poll_and_verify(graph, attached, command_id))
    return _stored_result(await _poll_and_verify(graph, attached, command_id))


def mark_abandoned_executions() -> int:
    """Mark startup-orphaned actions uncertain without releasing or replaying them."""
    graph = _graph("acme")
    rows = graph.query(
        "MATCH (a:ApprovalRecord) WHERE a.status IN ['executing','verifying'] "
        "OPTIONAL MATCH (i:Incident {tenant_id:a.tenant_id,incident_id:a.incident_id}) "
        "WITH a,collect(i) AS incidents,a.status AS prior "
        "SET a.status=CASE WHEN prior='executing' THEN 'execution_unknown' ELSE 'verification_unknown' END, "
        "a.abandoned_at=$now "
        "FOREACH (incident IN incidents | SET incident.status=CASE WHEN prior='executing' THEN 'execution_unknown' ELSE 'verification_unknown' END,incident.abandoned_at=$now) "
        "RETURN count(a)", {"now": _now()}, timeout=3000,
    ).result_set
    return int(rows[0][0]) if rows else 0
