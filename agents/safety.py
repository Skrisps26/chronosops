"""Deterministic, evidence-bound authority for incident proposals.

This module is deliberately the only place that turns a proposed action into an
approval-ready binding.  It re-reads graph and external evidence; agent prose is
never authority.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

from agents.mcp_client import FalkorDBMcpClient
from agents.settings import graph_names, require_tenant
from agents.tools import failover_routes
from agents.workflow import IncidentState
from integrations.aws_actions import AwsRecoveryClient
from integrations.monitoring import MonitoringClient
from integrations.slack import validate_slack_source


TOPOLOGY_QUERY = """
MATCH (m:Meta {id: 'ops_topology_meta'})
RETURN m.ready AS ready, m.hash AS topology_version
"""
REAL_EVIDENCE_QUERY = """
MATCH (i:Incident {incident_id:$incident_id, tenant_id:$tenant_id, service_id:$service_id,
                   execution_mode:'real', origin:'probe'})
MATCH (d:Deployment {deployment_id:$deployment_id, tenant_id:$tenant_id,
                     service_id:$service_id, active:true})
OPTIONAL MATCH (i)-[:HEALTH_EVIDENCE]->(h:HealthObservation)
WHERE h.id IN $observation_ids
RETURN properties(i) AS incident, properties(d) AS deployment,
       collect(properties(h)) AS observations
"""
SOURCE_QUERY = """
MATCH (s:SourceRecord {document_id:$document_id, service_id:$service_id,
                       status:'published', origin:'slack'})
RETURN properties(s) AS source
LIMIT 2
"""
OPEN_RECOVERY_QUERY = """
MATCH (r:RecoverySlot)
WHERE r.status IS NOT NULL AND NOT r.status IN ['free','released']
RETURN r.id AS recovery_id
UNION
MATCH (a:ApprovalRecord {tenant_id:$tenant_id, execution_mode:'real'})
WHERE a.status IN ['executing','verifying','execution_unknown','verification_unknown']
RETURN a.approval_id AS recovery_id
LIMIT 1
"""

FAULT_WINDOW_FILE = Path(__file__).resolve().parents[1] / "artifacts" / "fault-window.json"


class ProposalValidationError(RuntimeError):
    """Base class with a stable machine-readable failure reason."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


class ProposalConflict(ProposalValidationError):
    """Evidence exists but conflicts, is stale, foreign, or unsafe."""


class ProposalUnavailable(ProposalValidationError):
    """A required current evidence source could not be read."""


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ValidatedProposal:
    """Immutable refreshed facts that may be persisted for later approval."""

    proposed_action: Mapping[str, Any]
    topology_version: str
    incident_type: str
    deployment_binding: Mapping[str, Any]
    runbook_binding: Mapping[str, Any]
    fault_window: Mapping[str, Any]
    observation_ids: tuple[str, ...]
    evidence_versions: Mapping[str, Mapping[str, Any]]
    validated_at: str
    evidence_hash: str
    binding: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Return an ordinary JSON-ready projection for approval persistence."""
        return {
            "proposed_action": _thaw(self.proposed_action),
            "topology_version": self.topology_version,
            "incident_type": self.incident_type,
            "deployment_binding": _thaw(self.deployment_binding),
            "runbook_binding": _thaw(self.runbook_binding),
            "fault_window": _thaw(self.fault_window),
            "observation_ids": list(self.observation_ids),
            "evidence_versions": _thaw(self.evidence_versions),
            "validated_at": self.validated_at,
            "evidence_hash": self.evidence_hash,
            "binding": _thaw(self.binding),
        }


def _conflict(reason: str, detail: str) -> None:
    raise ProposalConflict(reason, detail)


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not value:
        _conflict("MISSING_EVIDENCE", f"{name} is required")
    return value


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        _conflict("MISSING_EVIDENCE", f"{name} is required")
    return value


def _stable_hash(binding: Mapping[str, Any]) -> str:
    payload = json.dumps(_thaw(binding), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

async def _query(client: FalkorDBMcpClient, graph: str, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        rows, _duration_ms = await client.query_readonly(graph, cypher, params, timeout_seconds=3.0, retries=0)
    except Exception as error:
        raise ProposalUnavailable("GRAPH_UNAVAILABLE", "required graph evidence could not be refreshed") from error
    if not isinstance(rows, list):
        raise ProposalUnavailable("GRAPH_UNAVAILABLE", "graph returned malformed evidence")
    return rows


async def _topology(client: FalkorDBMcpClient, required_version: str) -> str:
    if not required_version:
        _conflict("MISSING_EVIDENCE", "topology_version is required")
    rows = await _query(client, graph_names().topology, TOPOLOGY_QUERY, {})
    if len(rows) != 1 or rows[0].get("ready") is not True:
        _conflict("TOPOLOGY_UNAVAILABLE", "operational topology is not current and ready")
    current = rows[0].get("topology_version")
    if not isinstance(current, str) or not current:
        _conflict("TOPOLOGY_UNAVAILABLE", "operational topology has no version hash")
    if current != required_version:
        _conflict("STALE_TOPOLOGY", "proposal topology version does not match the current graph")
    return current


def _snapshot_matches(snapshot: Mapping[str, Any], current: Mapping[str, Any], fields: tuple[str, ...], name: str) -> None:
    for field in fields:
        expected = _require_text(snapshot.get(field), f"{name}.{field}")
        observed = current.get(field)
        if observed != expected:
            _conflict("BINDING_CONFLICT", f"{name}.{field} does not match refreshed evidence")


def _parse_time(value: Any, name: str) -> datetime:
    raw = _require_text(value, name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProposalConflict("INVALID_EVIDENCE", f"{name} is not a timestamp") from error
    if parsed.tzinfo is None:
        _conflict("INVALID_EVIDENCE", f"{name} has no timezone")
    return parsed.astimezone(timezone.utc)


def _read_fault_window() -> Mapping[str, Any]:
    path = FAULT_WINDOW_FILE
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ProposalUnavailable("FAULT_WINDOW_UNAVAILABLE", "fault-window metadata is absent") from error
    except (OSError, json.JSONDecodeError) as error:
        raise ProposalUnavailable("FAULT_WINDOW_UNAVAILABLE", "fault-window metadata cannot be read") from error
    if not isinstance(parsed, Mapping):
        raise ProposalUnavailable("FAULT_WINDOW_UNAVAILABLE", "fault-window metadata is malformed")
    return parsed


def _fault_binding(snapshot: Mapping[str, Any], deployment: Mapping[str, Any]) -> Mapping[str, Any]:
    live = _read_fault_window()
    required = ("deployment_id", "account_id", "region", "instance_id", "stack_id", "state", "watchdog_deadline")
    for field in required:
        expected = _require_text(snapshot.get(field), f"fault_window.{field}")
        actual = _require_text(live.get(field), f"live_fault_window.{field}")
        if expected != actual:
            _conflict("FAULT_WINDOW_CONFLICT", f"fault window {field} changed")
    for field in ("deployment_id", "account_id", "region", "instance_id", "stack_id"):
        if live[field] != deployment.get(field):
            _conflict("FAULT_WINDOW_CONFLICT", f"fault window {field} is foreign to deployment")
    if live["state"] != "active":
        _conflict("FAULT_WINDOW_INACTIVE", "fault watchdog window is not active")
    if (_parse_time(live["watchdog_deadline"], "fault_window.watchdog_deadline") - datetime.now(timezone.utc)).total_seconds() < 120:
        _conflict("FAULT_WINDOW_EXPIRING", "fault watchdog has less than 120 seconds remaining")
    return {field: live[field] for field in required}


def _slack_snapshot(versions: Mapping[str, Any], service_id: str) -> tuple[str, Mapping[str, Any]]:
    matches: list[tuple[str, Mapping[str, Any]]] = []
    for document_id, evidence in versions.items():
        if not isinstance(document_id, str) or not isinstance(evidence, Mapping):
            continue
        if evidence.get("origin") == "slack" and evidence.get("service_id") == service_id:
            matches.append((document_id, evidence))
    if len(matches) != 1:
        _conflict("MISSING_SLACK_EVIDENCE", "exactly one current Slack policy citation is required")
    document_id, evidence = matches[0]
    if document_id == "slack-004" or document_id.startswith("slack-004-"):
        _conflict("SYNTHETIC_SLACK_EVIDENCE", "synthetic Slack fixture is never live policy")
    for field in ("content_hash", "source_version", "origin", "service_id"):
        _require_text(evidence.get(field), f"evidence_versions.{document_id}.{field}")
    revision = evidence.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        _conflict("MISSING_SLACK_EVIDENCE", f"evidence_versions.{document_id}.revision is required")
    return document_id, evidence
async def _validate_slack(client: FalkorDBMcpClient, state: IncidentState) -> Mapping[str, Mapping[str, Any]]:
    versions = state.evidence_versions
    document_id, snapshot = _slack_snapshot(versions, state.service_id)
    rows = await _query(client, graph_names().company, SOURCE_QUERY, {"document_id": document_id, "service_id": state.service_id})
    if len(rows) != 1 or not isinstance(rows[0].get("source"), Mapping):
        _conflict("MISSING_SLACK_EVIDENCE", "cited Slack policy is absent or ambiguous")
    source = rows[0]["source"]
    _snapshot_matches(snapshot, source, ("content_hash", "source_version", "origin", "service_id"), "slack_source")
    if source.get("revision") != snapshot["revision"]:
        _conflict("SLACK_CONFLICT", "cited Slack policy revision changed")
    try:
        remote = await validate_slack_source(document_id, snapshot["content_hash"], str(snapshot["source_version"]))
    except Exception as error:
        raise ProposalUnavailable("SLACK_UNAVAILABLE", "cited Slack policy cannot be currently verified") from error
    if not remote.get("valid"):
        code = remote.get("error")
        if code in {"unverifiable", "rate_limited", "deleted_or_unavailable"}:
            raise ProposalUnavailable("SLACK_UNAVAILABLE", "cited Slack policy cannot be currently verified")
        _conflict("SLACK_CONFLICT", "cited Slack policy changed, is deleted, or is foreign")
    return {document_id: {field: source[field] for field in ("content_hash", "source_version", "revision", "origin", "service_id")}}


async def _real_proposal(state: IncidentState, client: FalkorDBMcpClient, topology_version: str) -> ValidatedProposal:
    if os.environ.get("REAL_RECOVERY_ENABLED", "false").lower() != "true":
        _conflict("REAL_RECOVERY_DISARMED", "real recovery is globally disabled")
    if not state.evidence:
        _conflict("MISSING_EVIDENCE", "real proposal has no evidence snapshot")
    deployment_snapshot = _require_mapping(state.deployment_binding, "deployment_binding")
    runbook_snapshot = _require_mapping(state.runbook_binding, "runbook_binding")
    action = _require_mapping(state.proposed_action, "proposed_action")
    if action.get("action_kind") != "restart_service":
        _conflict("UNSUPPORTED_ACTION", "real proposal must be a restart_service")
    observations = tuple(_require_text(item, "observation_id") for item in state.observation_ids)
    if not observations or len(set(observations)) != len(observations):
        _conflict("MISSING_EVIDENCE", "unique persisted observation_ids are required")
    deployment_id = _require_text(deployment_snapshot.get("deployment_id"), "deployment_binding.deployment_id")
    rows = await _query(client, graph_names().topology, REAL_EVIDENCE_QUERY, {
        "incident_id": state.incident_id,
        "tenant_id": state.tenant_id,
        "service_id": state.service_id,
        "deployment_id": deployment_id,
        "observation_ids": list(observations),
    })
    if len(rows) != 1 or not isinstance(rows[0].get("incident"), Mapping) or not isinstance(rows[0].get("deployment"), Mapping):
        _conflict("INCIDENT_NOT_CONFIRMED", "no exact monitored incident and active deployment were found")
    incident = rows[0]["incident"]
    deployment = rows[0]["deployment"]
    _snapshot_matches(deployment_snapshot, deployment, (
        "deployment_id", "tenant_id", "service_id", "account_id", "region", "instance_id", "stack_id",
        "document_name", "document_version", "document_hash", "recovery_role_arn", "operator", "expires_at", "endpoint",
    ), "deployment_binding")
    if _parse_time(deployment["expires_at"], "deployment.expires_at") <= datetime.now(timezone.utc):
        _conflict("DEPLOYMENT_EXPIRED", "the controlled deployment has expired")
    if incident.get("deployment_id") != deployment_id or incident.get("status") in {"recovered", "recovered_externally", "resolved"}:
        _conflict("INCIDENT_NOT_CONFIRMED", "incident is not an active exact deployment failure")
    graph_observations = rows[0].get("observations")
    if not isinstance(graph_observations, list) or len(graph_observations) != len(observations):
        _conflict("OBSERVATION_CONFLICT", "persisted probe observations are missing or ambiguous")
    by_id = {item.get("id"): item for item in graph_observations if isinstance(item, Mapping)}
    if set(by_id) != set(observations):
        _conflict("OBSERVATION_CONFLICT", "persisted probe observation IDs changed")
    if any(item.get("origin") != "probe" or item.get("deployment_id") != deployment_id or item.get("exporter_up") is not True or item.get("probe_success") is not False or not isinstance(item.get("http_status"), int) or not 0 <= item["http_status"] <= 599 for item in by_id.values()):
        _conflict("OBSERVATION_CONFLICT", "persisted observations do not prove a failed HTTP probe with a healthy exporter")
    try:
        assessment = await asyncio.to_thread(
            MonitoringClient(deployment_id=deployment_id).check_health, deployment.get("endpoint")
        )
    except Exception as error:
        raise ProposalUnavailable("MONITORING_UNAVAILABLE", "current monitoring evidence could not be read") from error
    samples = getattr(assessment, "samples", [])
    if getattr(assessment, "status", None) == "monitoring_unknown":
        raise ProposalUnavailable("MONITORING_UNAVAILABLE", "current monitoring is disconnected or uncertain")
    if getattr(assessment, "status", None) != "unhealthy" or not samples:
        _conflict("MONITORING_CONFLICT", "current monitoring does not confirm an HTTP failure")
    if any(
        getattr(sample, "exporter_up", None) is not True
        or getattr(sample, "probe_success", None) is not False
        or not isinstance(getattr(sample, "http_status", None), int)
        or not 0 <= sample.http_status <= 599
        for sample in samples
    ):
        _conflict("MONITORING_CONFLICT", "current probe samples do not prove failed HTTP with a healthy exporter")
    current_ids = {getattr(sample, "id", None) for sample in samples}
    if not current_ids <= set(observations):
        _conflict("OBSERVATION_CONFLICT", "current probe evidence was not persisted with the incident")
    _snapshot_matches(runbook_snapshot, deployment, ("document_name", "document_version", "document_hash", "recovery_role_arn"), "runbook_binding")
    if any(action.get(key) != deployment[key] for key in ("instance_id", "document_name")) or any(
        key in action and action[key] != deployment[key]
        for key in ("deployment_id", "service_id", "document_version", "document_hash")
    ):
        _conflict("TARGET_CONFLICT", "proposed restart target is not the refreshed deployment binding")
    try:
        aws = await AwsRecoveryClient(region=deployment["region"]).refresh_binding(deployment)
    except Exception as error:
        raise ProposalUnavailable("AWS_BINDING_UNAVAILABLE", "AWS target, role, EC2, SSM, or runbook could not be refreshed") from error
    for field in ("account_id", "region", "instance_id", "document_name", "document_version", "document_hash", "recovery_role_arn"):
        if aws.get(field) != deployment[field]:
            _conflict("AWS_BINDING_CONFLICT", f"AWS {field} changed from the published binding")
    no_open = await _query(client, graph_names().topology, OPEN_RECOVERY_QUERY, {"tenant_id": state.tenant_id})
    if no_open:
        _conflict("RECOVERY_IN_PROGRESS", "an unresolved recovery slot exists")
    fault = _fault_binding(_require_mapping(state.fault_window, "fault_window"), deployment)
    sources = await _validate_slack(client, state)
    refreshed_action = {key: deployment[key] for key in
                        ("deployment_id", "service_id", "instance_id", "document_name", "document_version", "document_hash")}
    refreshed_action["action_kind"] = "restart_service"
    binding = {
        "tenant_id": state.tenant_id, "incident_id": state.incident_id, "service_id": state.service_id,
        "execution_mode": "real", "action": refreshed_action, "topology_version": topology_version,
        "incident_type": state.incident_type,
        "incident": {key: incident[key] for key in ("incident_id", "tenant_id", "service_id", "deployment_id", "execution_mode", "origin", "starts_at") if key in incident},
        "deployment": {key: deployment[key] for key in (
            "deployment_id", "tenant_id", "service_id", "account_id", "region", "instance_id", "stack_id",
            "document_name", "document_version", "document_hash", "recovery_role_arn", "operator", "expires_at", "endpoint",
        )}, "runbook": {key: deployment[key] for key in ("document_name", "document_version", "document_hash", "recovery_role_arn")},
        "fault_window": fault, "observation_ids": sorted(observations),
        "observations": [{key: observation[key] for key in ("id", "data_hash", "observed_at", "probe_success", "http_status", "exporter_up") if key in observation} for observation in sorted(by_id.values(), key=lambda item: item["id"])],
        "evidence_versions": sources,
    }
    return _proposal(refreshed_action, topology_version, state.incident_type, binding)


async def _simulation_proposal(state: IncidentState, topology_version: str) -> ValidatedProposal:
    action = _require_mapping(state.proposed_action, "proposed_action")
    if action.get("action_kind") != "simulate_failover":
        _conflict("UNSUPPORTED_ACTION", "simulation proposal must be simulate_failover")
    route = action.get("route")
    if not isinstance(route, list) or len(route) < 2 or any(not isinstance(node, str) or not node for node in route):
        _conflict("INVALID_ROUTE", "proposal must contain a nonempty source-to-target route")
    try:
        result = await failover_routes(route[0], route[-1], state.max_cost, tenant=state.tenant_id)
    except Exception as error:
        raise ProposalUnavailable("ROUTE_UNAVAILABLE", "bounded failover route refresh is unavailable") from error
    if result.status in {"ERROR", "NOT_READY", "QUERY_TIMEOUT"}:
        raise ProposalUnavailable("ROUTE_UNAVAILABLE", "bounded failover route refresh is unavailable")
    if result.status != "OK" or not isinstance(result.data, Mapping):
        _conflict("ROUTE_UNSAFE", "no current eligible failover route exists")
    data = result.data
    if data.get("topology_version") != topology_version or data.get("source") != route[0] or data.get("target") != route[-1] or data.get("max_cost_cap") != state.max_cost:
        _conflict("ROUTE_CONFLICT", "refreshed route has incompatible topology or bounds")
    safe_routes = result.evidence.get("safe_routes") if isinstance(result.evidence, Mapping) else None
    exclusions = result.evidence.get("exclusions") if isinstance(result.evidence, Mapping) else None
    if not isinstance(safe_routes, list) or not isinstance(exclusions, Mapping):
        _conflict("ROUTE_EVIDENCE_MISSING", "route health and exclusion evidence is required")
    if exclusions.get("topology_version") != topology_version or exclusions.get("max_cost") != state.max_cost:
        _conflict("ROUTE_EXCLUSIONS_STALE", "route exclusions do not match current topology and cap")
    source_versions = exclusions.get("source_versions")
    if not isinstance(source_versions, Mapping) or source_versions.get("topology") != topology_version:
        _conflict("ROUTE_EXCLUSIONS_STALE", "route exclusion source version is missing or stale")
    selected = next((candidate for candidate in safe_routes if isinstance(candidate, Mapping) and candidate.get("route") == route), None)
    if selected is None or selected.get("cost") != action.get("cost") or not isinstance(selected.get("cost"), int) or selected["cost"] > state.max_cost:
        _conflict("ROUTE_CONFLICT", "proposal route or cost is not a current eligible candidate")
    if selected.get("node_health") is not True or selected.get("edge_health") is not True:
        _conflict("ROUTE_UNSAFE", "selected route has unhealthy or unknown topology")
    refreshed_action = {"action_kind": "simulate_failover", "route": list(route), "cost": selected["cost"]}
    binding = {
        "tenant_id": state.tenant_id, "incident_id": state.incident_id, "service_id": state.service_id,
        "execution_mode": "simulation", "action": refreshed_action, "topology_version": topology_version,
        "incident_type": state.incident_type, "max_cost": state.max_cost, "route": list(route),
        "route_evidence": {"node_health": True, "edge_health": True, "exclusions": dict(exclusions)},
    }
    return _proposal(refreshed_action, topology_version, state.incident_type, binding)


def _proposal(action: Mapping[str, Any], topology_version: str, incident_type: str, binding: Mapping[str, Any]) -> ValidatedProposal:
    frozen_binding = _freeze(binding)
    return ValidatedProposal(
        proposed_action=_freeze(action), topology_version=topology_version, incident_type=incident_type,
        deployment_binding=_freeze(binding.get("deployment", {})), runbook_binding=_freeze(binding.get("runbook", {})),
        fault_window=_freeze(binding.get("fault_window", {})), observation_ids=tuple(binding.get("observation_ids", ())),
        evidence_versions=_freeze(binding.get("evidence_versions", {})), validated_at=datetime.now(timezone.utc).isoformat(),
        evidence_hash=_stable_hash(binding), binding=frozen_binding,
    )


async def validate_proposal(state: IncidentState) -> ValidatedProposal:
    """Refresh every authoritative fact and return an immutable approval binding.

    Any absent, stale, ambiguous, or unrefreshable fact raises a machine-readable
    domain error instead of returning a partially safe proposal.
    """
    try:
        require_tenant(state.tenant_id)
    except ValueError:
        _conflict("FOREIGN_TENANT", "proposal tenant is not configured")
    if state.execution_mode == "real" and state.tenant_id != "acme":
        _conflict("FOREIGN_TENANT", "only acme owns the real workload")
    if not state.incident_id or not state.service_id:
        _conflict("MISSING_EVIDENCE", "incident_id and service_id are required")
    if not state.incident_type:
        _conflict("MISSING_EVIDENCE", "incident_type is required")
    client = FalkorDBMcpClient(tenant=state.tenant_id)
    topology_version = await _topology(client, state.topology_version)
    services = await _query(client, graph_names().topology,
                            "MATCH (s:Service {service_id:$service_id}) RETURN s.service_id AS service_id LIMIT 2",
                            {"service_id": state.service_id})
    if len(services) != 1:
        _conflict("SERVICE_NOT_FOUND", "the incident service is unknown or ambiguous")
    if state.execution_mode == "real":
        return await _real_proposal(state, client, topology_version)
    return await _simulation_proposal(state, topology_version)
